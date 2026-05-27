import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, TimeEntry
from app.models._enums import EntrySource

SUPPORTED_TARGETS = {
    "entry_date", "start_time", "end_time",
    "duration_minutes", "duration_hours",
    "project_code", "description", "tags",
    "sync_target", "external_ref",
}


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
) -> dict:
    bad_targets = set(column_map.values()) - SUPPORTED_TARGETS
    if bad_targets:
        raise ValueError(f"Unsupported target fields: {sorted(bad_targets)}")

    text = raw_bytes.decode(encoding)
    reader = csv.DictReader(io.StringIO(text), delimiter=separator)

    created_ids: list[int] = []
    errors: list[dict] = []
    project_cache: dict[str, Project | None] = {}

    def get_project(code: str | None) -> Project | None:
        if not code:
            return None
        if code in project_cache:
            return project_cache[code]
        proj = db.execute(select(Project).where(Project.code == code)).scalar_one_or_none()
        project_cache[code] = proj
        return proj

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
            project = get_project(code)
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
    return {"created": len(created_ids), "failed": len(errors), "ids": created_ids, "errors": errors}
