import csv
import io
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Project, TimeEntry
from app.models._enums import EntrySource, SyncTarget
from app.services import sync_fields as sf
from app.services.entry_sync import reconcile_entry_syncs
from app.services.sync_rules import load_rules
from app.services.transforms import (
    apply_transforms,
    auto_duration_to_minutes,
    clock_duration_to_minutes,
    eval_target_rules,
    humanized_duration_to_minutes,
)


def _parse_duration_field(value: str, *, as_hours: bool) -> int | None:
    """Turn an explicit duration cell into whole minutes. Clock formats like
    "01:30:00" / "01:30" are read as HH:MM(:SS); otherwise the value is numeric —
    minutes, or decimal hours when it came from a duration_hours column."""
    v = (value or "").strip()
    if not v:
        return None
    if ":" in v:
        return clock_duration_to_minutes(v)
    try:
        num = float(v.replace(",", "."))
    except ValueError:
        return None
    return int(round(num * 60)) if as_hours else int(num)

_BASE_TARGETS = {
    "entry_date", "start_time", "end_time",
    "duration", "duration_minutes", "duration_hours", "duration_human",
    "project_code", "customer", "description", "tags",
    "sync_target", "external_ref",
}
# Entry-level sync fields (e.g. sync:jira.issue_key) are valid mapping targets too.
SUPPORTED_TARGETS = _BASE_TARGETS | sf.entry_field_targets()
_KNOWN_SYNC_TARGETS = {t.value for t in SyncTarget}


def _normalize_header(h: str) -> str:
    """Strip BOM and surrounding whitespace from a CSV header. The UTF-8 BOM
    sneaks into headers when a tool like Toggl exports with byte-order-mark
    and the first column is quoted — the csv module then yields the literal
    `\\ufeff"Description"` as the first key, and our mapping lookup misses."""
    return h.lstrip("﻿").strip()


def _normalize_code(code: str) -> str:
    """Loose key for matching project codes: strip, upper, collapse whitespace
    and dashes/underscores."""
    return re.sub(r"[\s_-]+", "", code.strip().upper())


def import_csv(
    db: Session,
    user_id: int,
    raw_bytes: bytes,
    *,
    column_map: dict[str, str],
    default_project_code: str | None = None,
    separator: str = ";",
    encoding: str = "utf-8",
    date_format: str = "%Y-%m-%d",
    time_format: str = "%H:%M",
    auto_create_projects: bool = True,
    transforms: list[dict] | None = None,
    target_rules: list[dict] | None = None,
    apply_target_rules: bool = False,
) -> dict:
    bad_targets = set(column_map.keys()) - SUPPORTED_TARGETS
    if bad_targets:
        raise ValueError(f"Unsupported target fields: {sorted(bad_targets)}")

    # utf-8-sig transparently strips a leading BOM if present, and is harmless
    # if it's not — so we upgrade plain "utf-8" to handle exports from tools
    # that always emit one (Excel, Toggl on Windows, ...).
    effective_encoding = "utf-8-sig" if encoding.lower() in {"utf-8", "utf8"} else encoding
    text = raw_bytes.decode(effective_encoding)
    reader = csv.DictReader(io.StringIO(text), delimiter=separator)
    # Even with utf-8-sig some files still contain a literal BOM mid-stream or
    # have leading whitespace on headers; normalize defensively.
    if reader.fieldnames:
        reader.fieldnames = [_normalize_header(h) for h in reader.fieldnames]

    # column_map is target-keyed ({target: source}); normalize the source values
    # so they compare apples to apples with the (normalized) row headers.
    column_map = {target: _normalize_header(src) for target, src in column_map.items()}

    created_ids: list[int] = []
    errors: list[dict] = []
    created_projects: list[str] = []
    project_cache: dict[str, Project] = {}
    # Indices of existing projects (scoped to the importing user, since projects
    # are per-user) for loose matching. Code is the primary key; name is a
    # fallback so a Clockify tag carrying the *project name* (e.g. "Acme Corp")
    # still routes to the existing project instead of auto-creating a duplicate.
    # "ACME 1" matches "acme-1" via the normalized form.
    norm_index: dict[str, Project] = {}
    name_index: dict[str, Project] = {}
    for p in db.execute(select(Project).where(Project.user_id == user_id)).scalars():
        norm_index[_normalize_code(p.code)] = p
        # First project wins on a name collision; don't clobber a real match.
        name_index.setdefault(_normalize_code(p.name or ""), p)

    def get_or_create_project(code: str | None, customer: str | None = None) -> Project | None:
        if not code:
            return None
        code = code.strip()
        if not code:
            return None
        cust = (customer or "").strip() or None
        cached = project_cache.get(code)
        if cached is not None:
            # Backfill the customer if the row was first seen empty and the
            # current CSV row supplies one.
            if cust and not cached.customer:
                cached.customer = cust
                db.add(cached)
            return cached
        normalized = _normalize_code(code)
        existing = norm_index.get(normalized)
        if existing is None:
            existing = db.execute(
                select(Project).where(
                    Project.user_id == user_id, func.upper(Project.code) == code.upper()
                )
            ).scalar_one_or_none()
        # Fall back to a name match (tag = project name convention).
        if existing is None and normalized:
            existing = name_index.get(normalized)
        if existing is None and auto_create_projects:
            existing = Project(code=code, name=code, customer=cust, user_id=user_id)
            db.add(existing)
            db.flush()
            norm_index[normalized] = existing
            name_index.setdefault(normalized, existing)
            created_projects.append(code)
        elif existing is not None and cust and not existing.customer:
            existing.customer = cust
            db.add(existing)
        if existing is not None:
            project_cache[code] = existing
        return existing

    rules = load_rules(db)
    for row_no, raw_row in enumerate(reader, start=2):
        try:
            mapped: dict = {}
            for target, src in column_map.items():
                if src in raw_row:
                    mapped[target] = raw_row[src]

            # Per-column transforms override / derive values (regex, date, split, ...).
            for tgt, val in apply_transforms(
                transforms, raw_row, date_format=date_format, supported=SUPPORTED_TARGETS
            ).items():
                mapped[tgt] = val

            # Route namespaced sync fields into the entry's sync_metadata_override.
            sync_meta: dict[str, dict] = {}
            for key in [k for k in mapped if k.startswith("sync:")]:
                parsed = sf.parse_target_token(key)
                if parsed:
                    t, fk = parsed
                    sync_meta.setdefault(t, {})[fk] = mapped.pop(key)

            entry_date_str = mapped.get("entry_date")
            if not entry_date_str:
                raise ValueError("entry_date missing")
            entry_date = datetime.strptime(entry_date_str.strip(), date_format).date()

            start_time = end_time = None
            if mapped.get("start_time"):
                start_time = datetime.strptime(mapped["start_time"].strip(), time_format).time()
            if mapped.get("end_time"):
                end_time = datetime.strptime(mapped["end_time"].strip(), time_format).time()

            duration = None
            if mapped.get("duration"):
                duration = auto_duration_to_minutes(mapped["duration"])
            elif mapped.get("duration_human"):
                duration = humanized_duration_to_minutes(mapped["duration_human"])
            elif mapped.get("duration_minutes"):
                duration = _parse_duration_field(mapped["duration_minutes"], as_hours=False)
            elif mapped.get("duration_hours"):
                duration = _parse_duration_field(mapped["duration_hours"], as_hours=True)
            elif start_time and end_time:
                duration = (end_time.hour * 60 + end_time.minute) - (
                    start_time.hour * 60 + start_time.minute
                )

            if duration is None or duration <= 0:
                raise ValueError("could not derive a positive duration")

            code = mapped.get("project_code") or default_project_code
            project = get_or_create_project(code, mapped.get("customer"))
            if project is None:
                raise ValueError(f"unknown project_code '{code}'")

            tags_raw = mapped.get("tags") or ""
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

            # Effective sync target: an explicit mapped column wins; otherwise a
            # matching target rule (when enabled) may set it.
            explicit = (mapped.get("sync_target") or "").strip() or None
            rule_target = (
                eval_target_rules(target_rules, mapped, sync_meta, raw_row)
                if apply_target_rules
                else None
            )
            candidate = explicit or rule_target
            sync_target_override = candidate if candidate in _KNOWN_SYNC_TARGETS else None

            entry = TimeEntry(
                user_id=user_id,
                project_id=project.id,
                entry_date=entry_date,
                start_time=start_time,
                end_time=end_time,
                duration_minutes=duration,
                description=(mapped.get("description") or "").strip(),
                tags=tags,
                sync_target_override=sync_target_override,
                sync_metadata_override=sync_meta,
                external_ref=(mapped.get("external_ref") or None),
                source=EntrySource.CSV,
            )
            db.add(entry)
            db.flush()
            reconcile_entry_syncs(db, entry, project, rules)
            created_ids.append(entry.id)
        except Exception as e:  # noqa: BLE001
            errors.append({"row": row_no, "error": str(e), "data": raw_row})

    db.commit()
    return {
        "created": len(created_ids),
        "failed": len(errors),
        "ids": created_ids,
        "errors": errors,
        "created_projects": created_projects,
    }
