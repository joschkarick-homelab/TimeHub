"""Per-column import transforms and conditional target rules (Phase 2).

Transforms turn one raw CSV cell into a target value: copy it as-is, pull a
substring via regex, reformat a date, split on a separator, or inject a
constant. Target tokens are TimeHub fields (``description``, ``entry_date``,
...) or namespaced sync fields (``sync:jira.issue_key``).

Target rules optionally set an entry's sync target when the right source info
is present — e.g. once a Jira ticket has been extracted, route the entry to
Jira automatically.

Everything here is pure and import-only; export still uses the plain
column_map so formats stay round-trippable.
"""

from __future__ import annotations

import re
from datetime import datetime

# Bound the input we run user/AI-supplied regex against, to keep a pathological
# pattern from melting down on a huge cell (cheap ReDoS mitigation — Python's
# re has no timeout).
_MAX_REGEX_INPUT = 2000
_PATTERN_CACHE: dict[str, re.Pattern] = {}

VALID_OPS = {"copy", "regex", "date", "split", "constant", "duration"}
_RULE_KEYS = ("source", "pattern", "group", "sep", "index", "value", "date_from", "default")


def clean_transforms(items: list | None, supported: set[str]) -> list[dict]:
    """Validate/normalize a list of transform rules, dropping anything that
    references an unknown target or operation. Shared by the web router (parsing
    the editor's hidden JSON) and the AI sanitizer."""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        op = (item.get("op") or "copy").lower()
        if target not in supported or op not in VALID_OPS:
            continue
        rule = {"target": target, "op": op}
        for k in _RULE_KEYS:
            if item.get(k) not in (None, ""):
                rule[k] = item[k]
        out.append(rule)
    return out


def clean_target_rules(items: list | None, known_targets: set[str]) -> list[dict]:
    """Validate/normalize conditional target rules."""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        set_target = item.get("set_target")
        if set_target not in known_targets:
            continue
        if item.get("when"):
            out.append({"when": item["when"], "set_target": set_target})
        elif item.get("when_source") and item.get("pattern"):
            out.append({
                "when_source": item["when_source"],
                "pattern": item["pattern"],
                "set_target": set_target,
            })
    return out


def _compiled(pattern: str) -> re.Pattern | None:
    rx = _PATTERN_CACHE.get(pattern)
    if rx is None:
        try:
            rx = re.compile(pattern)
        except re.error:
            return None
        _PATTERN_CACHE[pattern] = rx
    return rx


def safe_search(pattern: str, text: str | None) -> re.Match | None:
    if not pattern or not text:
        return None
    rx = _compiled(pattern)
    if rx is None:
        return None
    return rx.search(text[:_MAX_REGEX_INPUT])


def clock_duration_to_minutes(txt: str) -> int | None:
    """Parse a clock-style duration "HH:MM:SS" or "HH:MM" into whole minutes.
    Returns None for anything without a colon (too ambiguous to guess).
    Example: "01:30:00" -> 90 (1 hour 30 minutes 0 seconds)."""
    txt = (txt or "").strip()
    if ":" not in txt:
        return None
    parts = txt.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, s = nums
        return h * 60 + m + round(s / 60)
    if len(parts) == 2:
        h, m = nums
        return h * 60 + m
    return None


# Jira-style work-time units for the humanized "1w 2d 3h 4m" duration format:
# a work week is 5 work days, a work day is 8 work hours.
HOURS_PER_DAY = 8
DAYS_PER_WEEK = 5
_MINUTES_PER_DAY = HOURS_PER_DAY * 60
_MINUTES_PER_WEEK = DAYS_PER_WEEK * _MINUTES_PER_DAY
_HUMAN_UNIT_MINUTES = {"w": _MINUTES_PER_WEEK, "d": _MINUTES_PER_DAY, "h": 60, "m": 1}
_HUMAN_TOKEN_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([wdhm])", re.IGNORECASE)


def humanized_duration_to_minutes(txt: str) -> int | None:
    """Parse a Jira-style humanized duration like "1w 2d 3h 4m" into whole
    minutes, using work-time units (1w = 5d, 1d = 8h). Accepts any subset and
    order of week/day/hour/minute tokens ("3h", "2d 4h", "90m"); tokens may be
    space-separated or run together ("1w2d3h4m"). Returns None when the text
    holds no recognisable token or contains stray non-token characters."""
    v = (txt or "").strip()
    if not v:
        return None
    matches = list(_HUMAN_TOKEN_RE.finditer(v))
    if not matches:
        return None
    # Only whitespace may separate tokens — reject garbage like "1h banana 2m"
    # rather than silently swallowing the bits we happen to recognise.
    if _HUMAN_TOKEN_RE.sub("", v).strip():
        return None
    total = 0.0
    for num, unit in (m.groups() for m in matches):
        total += float(num.replace(",", ".")) * _HUMAN_UNIT_MINUTES[unit.lower()]
    return int(round(total))


def minutes_to_humanized(minutes: int) -> str:
    """Render whole minutes as a Jira-style "1w 2d 3h 4m" string using work-time
    units (1w = 5d, 1d = 8h). Emits only the non-zero parts, largest unit first;
    0 (or less) renders as "0m"."""
    minutes = int(minutes)
    if minutes <= 0:
        return "0m"
    weeks, rem = divmod(minutes, _MINUTES_PER_WEEK)
    days, rem = divmod(rem, _MINUTES_PER_DAY)
    hours, mins = divmod(rem, 60)
    parts = []
    for value, suffix in ((weeks, "w"), (days, "d"), (hours, "h"), (mins, "m")):
        if value:
            parts.append(f"{value}{suffix}")
    return " ".join(parts) if parts else "0m"


def auto_duration_to_minutes(value: str) -> int | None:
    """Normalize a duration in any common format to whole minutes:
    "01:30:00"/"01:30" → clock, "1w 2d 3h 4m"/"90m" → humanized work-time units,
    "1.5"/"1,5" → decimal hours, "90" → minutes."""
    v = (value or "").strip()
    if not v:
        return None
    if ":" in v:
        return clock_duration_to_minutes(v)
    # A unit letter (w/d/h/m) means the Jira-style humanized format; this takes
    # precedence over the bare-number paths so "1,5h" reads as 90, not 1.5.
    if re.search(r"[wdhm]", v, re.IGNORECASE):
        return humanized_duration_to_minutes(v)
    if "." in v or "," in v:
        try:
            return int(round(float(v.replace(",", ".")) * 60))
        except ValueError:
            return None
    try:
        return int(v)
    except ValueError:
        return None


def apply_transform(rule: dict, row: dict, *, date_format: str = "%Y-%m-%d") -> str | None:
    """Compute a single transform's output value, or None when it yields nothing.

    Date output is rendered back into ``date_format`` so the importer's
    downstream date parsing (which uses the format's date_format) still works.
    """
    op = (rule.get("op") or "copy").lower()
    src = rule.get("source") or ""
    raw = row.get(src) if src else ""
    if isinstance(raw, str):
        raw = raw.strip()

    out: str | None = None
    if op == "constant":
        out = rule.get("value") or ""
    elif op == "copy":
        out = raw or ""
    elif op == "regex":
        m = safe_search(rule.get("pattern") or "", raw or "")
        if m:
            try:
                out = m.group(int(rule.get("group", 1)))
            except (IndexError, ValueError):
                out = m.group(0)
    elif op == "split":
        sep = rule.get("sep") or ","
        try:
            idx = int(rule.get("index", 0) or 0)
        except (TypeError, ValueError):
            idx = 0
        parts = (raw or "").split(sep)
        if -len(parts) <= idx < len(parts):
            out = parts[idx].strip()
    elif op == "date":
        df = rule.get("date_from") or date_format
        try:
            out = datetime.strptime((raw or "").strip(), df).strftime(date_format)
        except ValueError:
            out = None
    elif op == "duration":
        # Accept any common duration format ("1:30", "90", "1,5", "1h 30m"),
        # matching the direct duration mapping and what the AI prompt promises.
        minutes = auto_duration_to_minutes(raw or "")
        if minutes is not None:
            if rule.get("target") == "duration_hours":
                out = f"{minutes / 60:.4f}".rstrip("0").rstrip(".")
            else:
                out = str(minutes)

    if not out and rule.get("default"):
        out = str(rule.get("default"))
    return out or None


def apply_transforms(
    transforms: list[dict] | None,
    row: dict,
    *,
    date_format: str,
    supported: set[str],
) -> dict[str, str]:
    """Apply all transforms, returning {target: value} for non-empty results."""
    out: dict[str, str] = {}
    for rule in transforms or []:
        target = rule.get("target")
        if not target or target not in supported:
            continue
        val = apply_transform(rule, row, date_format=date_format)
        if val:
            out[target] = val
    return out


def eval_target_rules(
    rules: list[dict] | None,
    mapped: dict,
    sync_meta: dict,
    row: dict,
) -> str | None:
    """First matching rule's set_target wins, else None.

    Rule forms:
      {"when": "<target token>", "set_target": "jira"}   # target value present
      {"when_source": "<header>", "pattern": "...", "set_target": "jira"}
    """
    for rule in rules or []:
        when = rule.get("when")
        if when:
            if when.startswith("sync:"):
                rest = when[len("sync:"):]
                if "." in rest:
                    t, k = rest.split(".", 1)
                    present = bool((sync_meta.get(t) or {}).get(k))
                else:
                    present = False
            else:
                present = bool(mapped.get(when))
            if present:
                return rule.get("set_target")
            continue
        wsrc = rule.get("when_source")
        if wsrc and rule.get("pattern") and safe_search(rule["pattern"], row.get(wsrc) or ""):
            return rule.get("set_target")
    return None
