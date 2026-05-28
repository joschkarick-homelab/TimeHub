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
