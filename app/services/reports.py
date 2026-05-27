import csv
import io
import json
from collections.abc import Iterable
from datetime import datetime, time

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


def _format_value(field: str, entry: TimeEntry, project: Project, user: User,
                  date_fmt: str, time_fmt: str) -> str:
    """Render a TimeHub target field as a string for CSV export."""
    if field == "entry_date":
        return entry.entry_date.strftime(date_fmt)
    if field == "start_time":
        return entry.start_time.strftime(time_fmt) if entry.start_time else ""
    if field == "end_time":
        return entry.end_time.strftime(time_fmt) if entry.end_time else ""
    if field == "duration_minutes":
        return str(entry.duration_minutes)
    if field == "duration_hours":
        return f"{entry.duration_minutes / 60:.2f}".replace(".", ",")
    if field == "project_code":
        return project.code
    if field == "description":
        return entry.description or ""
    if field == "tags":
        return ",".join(entry.tags or [])
    if field == "sync_target":
        return entry.sync_target_override or project.default_sync_target
    if field == "external_ref":
        return entry.external_ref or ""
    return ""


def export_via_import_format(
    rows: Iterable[Row],
    column_map: dict[str, str],
    *,
    separator: str = ",",
    encoding: str = "utf-8",
    date_format: str = "%Y-%m-%d",
    time_format: str = "%H:%M",
) -> tuple[str, str]:
    """Emit the same shape of CSV that the ImportFormat would consume.

    The ImportFormat's column_map is `source_header -> target_field`. To
    export, we walk the same mapping in reverse and emit a CSV with the
    original headers, filled from each entry's target_field value. This
    makes formats round-trippable: export A, re-import with the same format,
    end up with the same entries.

    If multiple source headers map to the same target field, only the first
    is emitted (the rest would be redundant duplicates).
    """
    if not column_map:
        raise ValueError("Format has no column mapping — cannot export")

    seen_targets: set[str] = set()
    columns: list[tuple[str, str]] = []  # (source_header, target_field)
    for src, target in column_map.items():
        if target in seen_targets:
            continue
        seen_targets.add(target)
        columns.append((src, target))

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=separator, quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\n")
    writer.writerow([src for src, _ in columns])
    for entry, project, user in rows:
        writer.writerow([
            _format_value(target, entry, project, user, date_format, time_format)
            for _src, target in columns
        ])
    return buf.getvalue(), encoding


def preview_via_import_format(
    raw_text: str,
    column_map: dict[str, str],
    *,
    separator: str = ",",
    date_format: str = "%Y-%m-%d",
    time_format: str = "%H:%M",
    max_rows: int = 5,
) -> tuple[list[dict], list[dict]]:
    """For the format-review screen: parse the first N rows of the user's
    sample and produce two parallel lists — the raw source row and what
    TimeHub would store after the column_map is applied. Used purely to
    show "source -> target" side-by-side so the user can sanity-check
    the mapping before saving."""
    reader = csv.DictReader(io.StringIO(raw_text), delimiter=separator)
    source_rows: list[dict] = []
    target_rows: list[dict] = []
    for raw_row in reader:
        if len(source_rows) >= max_rows:
            break
        source_rows.append({k: (v or "") for k, v in raw_row.items()})
        target: dict[str, str] = {}
        for src, field in column_map.items():
            val = (raw_row.get(src) or "").strip()
            if not val:
                continue
            if field == "entry_date":
                try:
                    target[field] = datetime.strptime(val, date_format).date().isoformat()
                except ValueError:
                    target[field] = f"⚠ {val} (Format?)"
            elif field in {"start_time", "end_time"}:
                try:
                    target[field] = datetime.strptime(val, time_format).time().isoformat(
                        timespec="minutes"
                    )
                except ValueError:
                    target[field] = f"⚠ {val} (Format?)"
            elif field == "duration_minutes":
                try:
                    target[field] = f"{int(float(val.replace(',', '.')))} min"
                except ValueError:
                    target[field] = f"⚠ {val}"
            elif field == "duration_hours":
                try:
                    target[field] = f"{float(val.replace(',', '.')):.2f} h"
                except ValueError:
                    target[field] = f"⚠ {val}"
            else:
                target[field] = val
        target_rows.append(target)
    return source_rows, target_rows


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
