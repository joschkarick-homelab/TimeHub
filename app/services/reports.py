import csv
import io
import json
from collections.abc import Iterable
from datetime import time

from app.models import CsvTemplate, Project, TimeEntry, User


def _row_dict(entry: TimeEntry, project: Project, user: User) -> dict:
    hours = round(entry.duration_minutes / 60, 4)
    target = entry.sync_target_override or project.default_sync_target
    return {
        "id": entry.id,
        "date": entry.entry_date.isoformat(),
        "start_time": entry.start_time.strftime("%H:%M") if entry.start_time else "",
        "end_time": entry.end_time.strftime("%H:%M") if entry.end_time else "",
        "duration_minutes": entry.duration_minutes,
        "duration_hours": hours,
        "project_code": project.code,
        "project_name": project.name,
        "customer": project.customer or "",
        "user_email": user.email,
        "user_name": user.full_name,
        "description": entry.description,
        "tags": ",".join(entry.tags or []),
        "sync_target": target,
        "sync_status": entry.sync_status,
        "external_ref": entry.external_ref or "",
    }


Row = tuple[TimeEntry, Project, User]


def to_json(rows: Iterable[Row]) -> str:
    return json.dumps([_row_dict(e, p, u) for e, p, u in rows], indent=2, ensure_ascii=False)


def to_markdown(rows: Iterable[Row]) -> str:
    headers = ["Datum", "Start", "Ende", "Std", "Projekt", "Kunde", "Consultant",
               "Ziel", "Beschreibung", "Tags"]
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    total = 0
    for e, p, u in rows:
        d = _row_dict(e, p, u)
        total += e.duration_minutes
        lines.append("| " + " | ".join([
            d["date"], d["start_time"], d["end_time"], f"{d['duration_hours']:.2f}",
            f"{d['project_code']} – {d['project_name']}", d["customer"], d["user_name"] or d["user_email"],
            d["sync_target"], d["description"].replace("\n", " ").replace("|", "\\|"), d["tags"],
        ]) + " |")
    lines.append("")
    lines.append(f"**Summe:** {total/60:.2f} h ({total} min)")
    return "\n".join(lines)


_STANDARD_CSV_COLUMNS = [
    ("Datum", "date"),
    ("Start", "start_time"),
    ("Ende", "end_time"),
    ("Dauer (h)", "duration_hours"),
    ("Projekt", "project_code"),
    ("Projektname", "project_name"),
    ("Kunde", "customer"),
    ("Consultant", "user_email"),
    ("Beschreibung", "description"),
    ("Tags", "tags"),
    ("SyncZiel", "sync_target"),
    ("ExtRef", "external_ref"),
]


def to_csv(rows: Iterable[Row], template: CsvTemplate | None = None) -> tuple[str, str]:
    rows = list(rows)
    if template is None:
        cols = _STANDARD_CSV_COLUMNS
        separator = ";"
        decimal = ","
    else:
        cols = [(c["header"], c["field"]) for c in template.columns]
        separator = template.separator
        decimal = template.decimal_separator

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=separator, quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\n")
    writer.writerow([h for h, _ in cols])
    for e, p, u in rows:
        d = _row_dict(e, p, u)
        out_row = []
        for _header, field in cols:
            val = d.get(field, "")
            if isinstance(val, float) and decimal != ".":
                val = f"{val:.2f}".replace(".", decimal)
            out_row.append(str(val))
        writer.writerow(out_row)
    encoding = template.encoding if template else "utf-8"
    return buf.getvalue(), encoding


def total_hours(rows: Iterable[Row]) -> float:
    return round(sum(e.duration_minutes for e, _, _ in rows) / 60, 2)


def parse_time_str(value: str) -> time | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(value, fmt).time()
        except ValueError:
            continue
    return None
