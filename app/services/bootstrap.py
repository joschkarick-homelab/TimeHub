"""First-run bootstrap: seed the canonical global import/export formats."""

import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ImportFormat

log = logging.getLogger(__name__)

# Canonical, ready-to-upload Jira worklog export. Seeded as a global format so
# every user can pick it without rebuilding the column mapping. The column_map
# is ordered to match the importer's expected columns
# (Ticket No, Start Date, Timespent, Comment); Timespent uses the humanized
# duration ("1h 30m") so Jira reads minutes correctly, and the date carries a
# real time of day in a locale-independent format.
JIRA_EXPORT_FORMAT_NAME = "Export für Jira"

_JIRA_EXPORT_SPEC = dict(
    source_hint="jira",
    separator=",",
    encoding="utf-8",
    date_format="%d-%b-%Y %H:%M:%S",
    time_format="%H:%M",
    column_map={
        "sync:jira.issue_key": "Ticket No",
        "entry_date": "Start Date",
        "duration_human": "Timespent",
        "description": "Comment",
    },
    notes="Direkt im Jira-Zeiterfassungs-Plugin hochladbar. Am besten mit "
          "dem Ziel-Filter Jira exportieren, damit nur Tickets enthalten sind.",
)


def ensure_builtin_formats() -> None:
    """Seed the canonical global import/export formats (idempotent).

    Only creates a format when one of the same name doesn't already exist as a
    global format, so an admin's later edits are never clobbered.
    """
    with SessionLocal() as db:
        exists = db.execute(
            select(ImportFormat.id).where(
                ImportFormat.name == JIRA_EXPORT_FORMAT_NAME,
                ImportFormat.is_global.is_(True),
            )
        ).first()
        if exists:
            return
        db.add(ImportFormat(name=JIRA_EXPORT_FORMAT_NAME, is_global=True, **_JIRA_EXPORT_SPEC))
        db.commit()
        log.info("Seeded global import format '%s'", JIRA_EXPORT_FORMAT_NAME)
