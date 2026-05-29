"""AI-assisted CSV column mapping.

Sends a small sample of the user's CSV to Claude and asks for a one-shot
mapping suggestion in strict JSON. The caller (UI / API) shows the suggestion
to the user for review before it is persisted as an ImportFormat.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re

from app.config import get_settings
from app.models._enums import SyncTarget
from app.schemas.import_format import SUPPORTED_TARGETS, ImportFormatSuggestion
from app.services import sync_fields as sf
from app.services.transforms import clean_target_rules, clean_transforms

log = logging.getLogger(__name__)

_KNOWN_SYNC_TARGETS = {t.value for t in SyncTarget}


class AiMappingError(RuntimeError):
    pass


SYSTEM_PROMPT = """\
You are a data-mapping assistant for TimeHub, a self-hosted time-tracking app for consultants.

Users will paste a small sample of a CSV file exported from a third-party time-tracking
tool (Toggl, Clockify, Harvest, Jira tempo, custom Excel exports, ...). Your job is to
infer how to import those rows into TimeHub.

Respond with STRICT JSON ONLY — no prose, no markdown fences, no comments — using exactly
this shape:

{
  "source_hint": "<short identifier, e.g. 'toggl', 'clockify', 'harvest', 'custom'>",
  "separator":   "<the CSV delimiter character, e.g. ',', ';' or '\\t'>",
  "encoding":    "utf-8",
  "date_format": "<Python strptime format for the date column, e.g. '%Y-%m-%d' or '%d.%m.%Y'>",
  "time_format": "<Python strptime format for time-of-day, e.g. '%H:%M' or '%H:%M:%S'>",
  "column_map":  { "<source CSV header exactly as written>": "<target field>", ... },
  "transforms":  [ <transform>, ... ],
  "target_rules":[ <target rule>, ... ],
  "default_project_code": null,
  "notes": "<one short German sentence explaining notable choices, may be empty>"
}

Target fields (use these names ONLY as mapping/transform targets, skip columns that don't fit):
  entry_date          required — the date of the work (calendar day)
  start_time          optional — wall-clock start, HH:MM
  end_time            optional — wall-clock end, HH:MM
  duration            PREFERRED — a duration in ANY format ("90", "1,5", "01:30:00"); unit auto-detected
  duration_minutes    only to force the value to be read as minutes
  duration_hours      only to force the value to be read as decimal hours
  project_code        the project's stable code/key
  description         free-text description / task / ticket title
  tags                comma-separated list of tags / labels
  sync_target         override sync target per row (jira / salesforce / bcs / intern / none)
  external_ref        any external reference id

A "transform" derives ONE target value from a source column (use when a plain
column_map copy is not enough — e.g. a ticket id buried in free text, or a date
in an unusual format). Shape:
  {
    "target": "<target field above OR a sync field below>",
    "source": "<source CSV header>",
    "op": "copy" | "regex" | "date" | "split" | "constant" | "duration",
    "pattern": "<regex with ONE capture group>",   // op=regex
    "group": 1,
    "date_from": "<strptime format of the source value>",  // op=date
    "sep": ",", "index": 0,                        // op=split (0-based)
    "value": "<fixed value>",                      // op=constant
    "default": "<fallback when the result is empty>"  // any op
  }
Example — pull "ABC-123" out of a Description like "Ticket ABC-123: did things":
  {"target":"sync:jira.issue_key","source":"Description","op":"regex","pattern":"([A-Z]+-\\d+)","group":1}

IMPORTANT about durations: a value like "01:30:00" (or "01:30") is a clock-style
duration meaning HH:MM:SS / HH:MM — i.e. 1 hour 30 minutes 0 seconds = 90 minutes,
NOT 1 minute. Never read the first field as minutes. PREFER mapping the column to the
single `duration` target (the unit is auto-detected for "90", "1,5" and "01:30:00").
Only use duration_minutes/duration_hours when you must force a specific unit. A
transform op is unnecessary for standard durations:
  {"<Duration column>": "duration"}   // in column_map

A "target rule" sets an entry's sync target automatically when info is present:
  { "when": "<a target field that, once filled, should trigger this>", "set_target": "jira|salesforce|bcs|intern|none" }
Example: { "when": "sync:jira.issue_key", "set_target": "jira" }

Rules:
- Map AT MOST ONE source column to each plain target field via column_map.
- Prefer duration_minutes/duration_hours over start_time+end_time only if the source has
  a single duration column.
- If a column doesn't have a clean mapping, omit it.
- Leave transforms/target_rules as empty arrays unless they clearly help.
- When the user sends a follow-up instruction, revise your PREVIOUS JSON to honor it and
  output the COMPLETE updated JSON again (same shape) — never a diff or prose.
- Output a single JSON object, nothing else.
"""


def _full_system_prompt() -> str:
    """Static prompt + the concrete entry-level sync-field tokens (stable across
    calls, so prompt caching still hits)."""
    lines = [f"  {tok}   ({sf.target_label(tok)})" for tok in sorted(sf.entry_field_targets())]
    return SYSTEM_PROMPT + "\nEntry-level sync field targets you may use:\n" + "\n".join(lines) + "\n"


def _ensure_sample(raw_text: str, max_lines: int) -> str:
    lines = raw_text.splitlines()
    sample = "\n".join(lines[: max(2, max_lines)])
    return sample[:8000]


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # tolerate accidental fencing despite the system prompt
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise AiMappingError(f"model did not return JSON: {text[:200]}") from e
        return json.loads(match.group(0))


def _clean_header(h: str) -> str:
    # Mirror app.services.csv_import._normalize_header so the keys we save in
    # column_map are byte-for-byte the same as what the importer will see in
    # row.keys() at import time (utf-8-sig strips BOM at decode anyway, but
    # the AI itself can return a BOM-prefixed first column).
    return h.lstrip("﻿").strip()


def _sanitize(raw: dict, raw_sample: str) -> ImportFormatSuggestion:
    column_map = raw.get("column_map") or {}
    cleaned: dict[str, str] = {}
    for src, target in column_map.items():
        if not isinstance(src, str) or not isinstance(target, str):
            continue
        if target in SUPPORTED_TARGETS:
            cleaned[_clean_header(src)] = target

    separator = str(raw.get("separator") or ",")[:4]
    # Re-detect headers using the separator the model chose, so quoted commas
    # in a semicolon-CSV don't fool us. Then union with the AI's keys — that
    # way every column the AI suggested is rendered as a row in the review UI,
    # even if our parser missed it.
    detected = _peek_headers(raw_sample, separator=separator if separator != "\\t" else "\t")
    seen = {h: True for h in detected}
    for src in cleaned:
        if src not in seen:
            detected.append(src)
            seen[src] = True

    return ImportFormatSuggestion(
        source_hint=str(raw.get("source_hint") or "custom")[:64],
        separator=separator,
        encoding=str(raw.get("encoding") or "utf-8")[:16],
        date_format=str(raw.get("date_format") or "%Y-%m-%d")[:32],
        time_format=str(raw.get("time_format") or "%H:%M")[:32],
        column_map=cleaned,
        transforms=clean_transforms(raw.get("transforms"), SUPPORTED_TARGETS),
        target_rules=clean_target_rules(raw.get("target_rules"), _KNOWN_SYNC_TARGETS),
        default_project_code=(raw.get("default_project_code") or None),
        notes=str(raw.get("notes") or "")[:1024],
        detected_headers=detected,
    )


def _peek_headers(sample: str, separator: str | None = None) -> list[str]:
    """Read the header row using the csv module so quoted fields parse
    cleanly. If `separator` is supplied we use it; otherwise we sniff
    the most likely one from the first line."""
    if not sample:
        return []
    if separator is None:
        first = sample.splitlines()[0]
        candidates = (",", ";", "\t", "|")
        separator = max(candidates, key=lambda s: first.count(s))
        if first.count(separator) == 0:
            return [first.strip()]
    reader = csv.reader(io.StringIO(sample), delimiter=separator)
    try:
        return [_clean_header(h) for h in next(reader)]
    except StopIteration:
        return []


def _build_messages(sample: str, max_lines: int, instruction: str | None, previous: dict | None) -> list[dict]:
    """Conversation for the model: always the CSV sample, then (for refinement)
    the previous JSON as the assistant turn and the user's new instruction."""
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Hier ist der Anfang der CSV (bis zu {max_lines} Zeilen):\n\n"
                f"```\n{sample}\n```\n\n"
                "Gib jetzt das JSON-Mapping aus."
            ),
        }
    ]
    if previous is not None:
        messages.append({"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)})
    if instruction:
        messages.append({
            "role": "user",
            "content": (
                f"Zusätzliche Anweisung des Nutzers:\n{instruction}\n\n"
                "Aktualisiere das Mapping entsprechend und gib das vollständige JSON erneut aus."
            ),
        })
    return messages


def suggest_mapping(
    raw_text: str,
    *,
    instruction: str | None = None,
    previous: dict | None = None,
    hints: str | None = None,
) -> ImportFormatSuggestion:
    """One-shot mapping suggestion, or a refinement when `instruction` + `previous`
    are given (the model revises its prior JSON to honor the instruction).

    `hints` are admin/user-configured standing instructions injected as a
    separate (uncached) system block, so the big base prompt stays cacheable."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AiMappingError(
            "AI suggestions are disabled. Set ANTHROPIC_API_KEY to enable, "
            "or fill the mapping manually."
        )

    sample = _ensure_sample(raw_text, settings.ai_mapping_max_sample_lines)

    # Import lazily so the app still boots when anthropic isn't installed
    from anthropic import Anthropic

    system: list[dict] = [
        {"type": "text", "text": _full_system_prompt(), "cache_control": {"type": "ephemeral"}}
    ]
    if hints and hints.strip():
        system.append({
            "type": "text",
            "text": "Verbindliche Vorgaben des Teams/Nutzers (immer beachten):\n" + hints.strip(),
        })

    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.ai_mapping_model,
            max_tokens=1500,
            system=system,
            messages=_build_messages(
                sample, settings.ai_mapping_max_sample_lines, instruction, previous
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("anthropic call failed")
        raise AiMappingError(f"AI call failed: {e}") from e

    parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise AiMappingError("AI returned no text")
    raw = _extract_json("\n".join(parts))
    return _sanitize(raw, sample)
