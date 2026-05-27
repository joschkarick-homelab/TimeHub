"""AI-assisted CSV column mapping.

Sends a small sample of the user's CSV to Claude and asks for a one-shot
mapping suggestion in strict JSON. The caller (UI / API) shows the suggestion
to the user for review before it is persisted as an ImportFormat.
"""

from __future__ import annotations

import json
import logging
import re

from app.config import get_settings
from app.schemas.import_format import SUPPORTED_TARGETS, ImportFormatSuggestion

log = logging.getLogger(__name__)


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
  "default_project_code": null,
  "notes": "<one short German sentence explaining notable choices, may be empty>"
}

Target fields (use these names ONLY in column_map values, skip columns that don't fit):
  entry_date          required — the date of the work (calendar day)
  start_time          optional — wall-clock start, HH:MM
  end_time            optional — wall-clock end, HH:MM
  duration_minutes    integer minutes
  duration_hours      decimal hours (TimeHub will convert)
  project_code        the project's stable code/key
  description         free-text description / task / ticket title
  tags                comma-separated list of tags / labels
  sync_target         override sync target per row (jira / salesforce / bcs / intern / none)
  external_ref        any external reference id

Rules:
- Map AT MOST ONE source column to each target field.
- Prefer duration_minutes/duration_hours over start_time+end_time only if the source has
  a single duration column.
- If a column doesn't have a clean mapping, omit it from column_map.
- Output a single JSON object, nothing else.
"""


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


def _sanitize(raw: dict, sample_headers: list[str]) -> ImportFormatSuggestion:
    column_map = raw.get("column_map") or {}
    cleaned: dict[str, str] = {}
    for src, target in column_map.items():
        if not isinstance(src, str) or not isinstance(target, str):
            continue
        if target in SUPPORTED_TARGETS:
            cleaned[src] = target
    return ImportFormatSuggestion(
        source_hint=str(raw.get("source_hint") or "custom")[:64],
        separator=str(raw.get("separator") or ",")[:4],
        encoding=str(raw.get("encoding") or "utf-8")[:16],
        date_format=str(raw.get("date_format") or "%Y-%m-%d")[:32],
        time_format=str(raw.get("time_format") or "%H:%M")[:32],
        column_map=cleaned,
        default_project_code=(raw.get("default_project_code") or None),
        notes=str(raw.get("notes") or "")[:1024],
        detected_headers=sample_headers,
    )


def _peek_headers(sample: str) -> list[str]:
    first = sample.splitlines()[0] if sample else ""
    for sep in (";", ",", "\t", "|"):
        if sep in first:
            return [h.strip() for h in first.split(sep)]
    return [first.strip()] if first else []


def suggest_mapping(raw_text: str) -> ImportFormatSuggestion:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AiMappingError(
            "AI suggestions are disabled. Set ANTHROPIC_API_KEY to enable, "
            "or fill the mapping manually."
        )

    sample = _ensure_sample(raw_text, settings.ai_mapping_max_sample_lines)
    headers = _peek_headers(sample)

    # Import lazily so the app still boots when anthropic isn't installed
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.ai_mapping_model,
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Hier ist der Anfang der CSV (bis zu "
                        f"{settings.ai_mapping_max_sample_lines} Zeilen):\n\n"
                        f"```\n{sample}\n```\n\n"
                        "Gib jetzt das JSON-Mapping aus."
                    ),
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("anthropic call failed")
        raise AiMappingError(f"AI call failed: {e}") from e

    parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise AiMappingError("AI returned no text")
    raw = _extract_json("\n".join(parts))
    return _sanitize(raw, headers)
