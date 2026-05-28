"""Flexible grouping engine for the reporting page.

Takes the gathered (entry, project, user) rows and an ordered list of
grouping dimensions, and returns a nested tree of groups with subtotals.
Designed to grow: add a new entry to DIMENSIONS and it's immediately
available as a grouping option and inside presets.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

from app.models import Project, TimeEntry, User

Row = tuple[TimeEntry, Project, User]


def _week_key(d: date) -> tuple[str, str]:
    iso = d.isocalendar()
    key = f"{iso.year}-W{iso.week:02d}"
    return key, key


def _month_key(d: date) -> tuple[str, str]:
    key = d.strftime("%Y-%m")
    return key, d.strftime("%B %Y")


# Each dimension maps a row to (sort_key, human_label).
DIMENSIONS: dict[str, Callable[[Row], tuple[str, str]]] = {
    "day": lambda r: (r[0].entry_date.isoformat(), r[0].entry_date.isoformat()),
    "week": lambda r: _week_key(r[0].entry_date),
    "month": lambda r: _month_key(r[0].entry_date),
    "project": lambda r: (r[1].code, r[1].display_label),
    "customer": lambda r: ((r[1].customer or "—"), (r[1].customer or "— ohne Kunde —")),
    "user": lambda r: (
        (r[2].full_name or r[2].email),
        (r[2].full_name or r[2].email),
    ),
}

DIMENSION_LABELS = {
    "day": "Tag",
    "week": "Woche",
    "month": "Monat",
    "project": "Projekt",
    "customer": "Kunde",
    "user": "Mitarbeiter",
}


def _group(rows: list[Row], dims: list[str], level: int, detailed: bool) -> list[dict]:
    if not dims:
        return []
    dim = dims[0]
    keyfn = DIMENSIONS[dim]
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        sort_key, label = keyfn(row)
        if sort_key not in buckets:
            buckets[sort_key] = {"label": label, "rows": []}
            order.append(sort_key)
        buckets[sort_key]["rows"].append(row)

    nodes: list[dict] = []
    is_leaf = len(dims) == 1
    for sort_key in sorted(order):
        bucket = buckets[sort_key]
        bucket_rows = bucket["rows"]
        total = sum(e.duration_minutes for e, _, _ in bucket_rows)
        node = {
            "dimension": dim,
            "dimension_label": DIMENSION_LABELS.get(dim, dim),
            "label": bucket["label"],
            "level": level,
            "total_minutes": total,
            "total_hours": round(total / 60, 2),
            "count": len(bucket_rows),
            "children": [],
            "entries": [],
        }
        if is_leaf:
            if detailed:
                node["entries"] = sorted(
                    bucket_rows, key=lambda r: (r[0].entry_date, r[0].id)
                )
        else:
            node["children"] = _group(bucket_rows, dims[1:], level + 1, detailed)
        nodes.append(node)
    return nodes


def build_report(
    rows: Iterable[Row], group_by: list[str], *, detailed: bool = False
) -> dict:
    """Return {"groups": [...nested...], "total_minutes", "total_hours", "count"}."""
    rows = list(rows)
    group_by = [d for d in group_by if d in DIMENSIONS] or ["day"]
    total = sum(e.duration_minutes for e, _, _ in rows)
    return {
        "groups": _group(rows, group_by, 0, detailed),
        "group_by": group_by,
        "detailed": detailed,
        "total_minutes": total,
        "total_hours": round(total / 60, 2),
        "count": len(rows),
    }


# Named presets. group_by is an ordered list; detailed shows leaf entries.
PRESETS: dict[str, dict] = {
    "weekly_detailed": {
        "label": "Wöchentlich – detailliert",
        "description": "Pro Woche und Tag, mit allen Einzeleinträgen.",
        "group_by": ["week", "day"],
        "detailed": True,
    },
    "weekly_day_project": {
        "label": "Wöchentlich – pro Tag & Projekt",
        "description": "Pro Woche, Tag und Projekt zusammengefasst (keine Einzeleinträge).",
        "group_by": ["week", "day", "project"],
        "detailed": False,
    },
    "monthly_project": {
        "label": "Monatlich – pro Projekt",
        "description": "Pro Monat und Projekt zusammengefasst.",
        "group_by": ["month", "project"],
        "detailed": False,
    },
    "by_customer": {
        "label": "Pro Kunde & Projekt",
        "description": "Über den ganzen Zeitraum nach Kunde und Projekt summiert.",
        "group_by": ["customer", "project"],
        "detailed": False,
    },
    "project_detailed": {
        "label": "Pro Projekt – detailliert",
        "description": "Pro Projekt, mit allen Einzeleinträgen.",
        "group_by": ["project"],
        "detailed": True,
    },
}
