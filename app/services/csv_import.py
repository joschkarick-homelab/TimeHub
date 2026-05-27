import csv
import io
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Project, TimeEntry
from app.models._enums import EntrySource

SUPPORTED_TARGETS = {
    "entry_date", "start_time", "end_time",
    "duration_minutes", "duration_hours",
    "project_code", "description", "tags",
    "sync_target", "external_ref",
}


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
) -> dict:
    bad_targets = set(column_map.values()) - SUPPORTED_TARGETS
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

    # Same normalization on the mapping side, so headers and mapping keys
    # compare apples to apples regardless of where the noise came from.
    column_map = {_normalize_header(src): target for src, target in column_map.items()}

    created_ids: list[int] = []
    errors: list[dict] = []
    created_projects: list[str] = []
    project_cache: dict[str, Project] = {}
    # Map of normalized code -> existing Project, so "ACME 1" matches "acme-1"
    norm_index: dict[str, Project] = {
        _normalize_code(p.code): p
        for p in db.execute(select(Project)).scalars()
    }

    def get_or_create_project(code: str | None) -> Project | None:
        if not code:
            return None
        code = code.strip()
        if not code:
            return None
        if code in project_cache:
            return project_cache[code]
        normalized = _normalize_code(code)
        existing = norm_index.get(normalized)
        if existing is None:
            # exact lookup as a final guard (case where index missed because the
            # DB has a row created after import started)
            existing = db.execute(
                select(Project).where(func.upper(Project.code) == code.upper())
            ).scalar_one_or_none()
        if existing is None and auto_create_projects:
            existing = Project(code=code, name=code)
            db.add(existing)
            db.flush()
            norm_index[normalized] = existing
            created_projects.append(code)
        if existing is not None:
            project_cache[code] = existing
        return existing

    for row_no, raw_row in enumerate(reader, start=2):
        try:
            mapped: dict = {}
            for src, target in column_map.items():
                if src in raw_row:
                    mapped[target] = raw_row[src]

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
            if mapped.get("duration_minutes"):
                duration = int(float(mapped["duration_minutes"].replace(",", ".")))
            elif mapped.get("duration_hours"):
                duration = int(round(float(mapped["duration_hours"].replace(",", ".")) * 60))
            elif start_time and end_time:
                duration = (end_time.hour * 60 + end_time.minute) - (
                    start_time.hour * 60 + start_time.minute
                )

            if duration is None or duration <= 0:
                raise ValueError("could not derive a positive duration")

            code = mapped.get("project_code") or default_project_code
            project = get_or_create_project(code)
            if project is None:
                raise ValueError(f"unknown project_code '{code}'")

            tags_raw = mapped.get("tags") or ""
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

            entry = TimeEntry(
                user_id=user_id,
                project_id=project.id,
                entry_date=entry_date,
                start_time=start_time,
                end_time=end_time,
                duration_minutes=duration,
                description=(mapped.get("description") or "").strip(),
                tags=tags,
                sync_target_override=(mapped.get("sync_target") or None),
                external_ref=(mapped.get("external_ref") or None),
                source=EntrySource.CSV,
            )
            db.add(entry)
            db.flush()
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
