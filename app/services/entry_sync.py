"""Materialize per-target sync state (Phase 0).

Turns an entry's resolved target set (see services.sync_rules) into concrete
`EntrySync` rows, and reconciles them when targets change. Already-completed
rows (synced / manually_synced) are never removed, so audit trail and remote
references survive a re-evaluation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import EntrySync
from app.models._enums import SyncStatus
from app.services import sync_fields as sf
from app.services.sync_rules import resolve_targets

# Rows in these states carry a remote reference / audit value and must not be
# deleted by reconciliation, even if the target no longer applies.
_PROTECTED = {SyncStatus.SYNCED, SyncStatus.MANUALLY_SYNCED}

# Targets shown as columns in the dashboard status matrix, in display order.
DISPLAY_TARGETS = ("jira", "bcs", "salesforce")
TARGET_LABELS = {"jira": "Jira", "bcs": "BCS", "salesforce": "Salesforce"}


def reconcile_entry_syncs(db: Session, entry, project, rules=()) -> list[str]:
    """Add `EntrySync` rows for newly-applicable targets and drop pending ones
    that no longer apply. Returns the resolved target list.

    The entry must already be flushed (have an id). The caller commits.
    """
    desired = resolve_targets(project, entry, rules)
    desired_set = set(desired)
    existing = {es.target: es for es in (entry.entry_syncs or [])}

    for target in desired_set - existing.keys():
        db.add(EntrySync(entry_id=entry.id, target=target, status=SyncStatus.PENDING))

    for target, es in existing.items():
        if target not in desired_set and es.status not in _PROTECTED:
            db.delete(es)

    return desired


def _find_row(entry, target) -> EntrySync | None:
    return next((s for s in (entry.entry_syncs or []) if s.target == target), None)


def set_target_status(
    db: Session, entry, target: str, status: str, *, external_ref: str | None = None,
    error: str | None = None,
) -> EntrySync:
    """Upsert the per-target sync row to a new state. Used to bridge the actual
    sync flows (Salesforce push, manual marking) into EntrySync so the status
    matrix reflects reality."""
    es = _find_row(entry, target)
    if es is None:
        es = EntrySync(entry_id=entry.id, target=target, status=status)
        db.add(es)
    else:
        es.status = status
    if external_ref is not None:
        es.external_ref = external_ref
    if status == SyncStatus.FAILED:
        es.attempts = (es.attempts or 0) + 1
        es.last_error = error
    elif status in _PROTECTED:
        es.synced_at = datetime.now(timezone.utc)
        es.last_error = None
    db.add(es)
    return es


def mark_all_manually_synced(db: Session, entry) -> None:
    """Flag every not-yet-done target of an entry as manually handled."""
    for es in (entry.entry_syncs or []):
        if es.status not in _PROTECTED:
            es.status = SyncStatus.MANUALLY_SYNCED
            es.synced_at = datetime.now(timezone.utc)
            es.last_error = None
            db.add(es)


def unmark_manually_synced(db: Session, entry) -> None:
    """Revert manual marks back to pending (real syncs are left untouched)."""
    for es in (entry.entry_syncs or []):
        if es.status == SyncStatus.MANUALLY_SYNCED:
            es.status = SyncStatus.PENDING
            es.synced_at = None
            db.add(es)


def matrix_cell(entry, project, target: str) -> dict:
    """Status-matrix cell for one entry × target: traffic-light state + tooltip.

    grey  = target not applicable (no row) or skipped
    green = synced / manually synced
    yellow= applicable, ready, still pending
    red   = applicable but blocked (missing fields) or last push failed
    """
    es = _find_row(entry, target)
    if es is None:
        return {"target": target, "state": "grey", "tooltip": "Nicht relevant"}
    if es.status == SyncStatus.SYNCED:
        return {"target": target, "state": "green", "tooltip": "Synchronisiert"}
    if es.status == SyncStatus.MANUALLY_SYNCED:
        return {"target": target, "state": "green", "tooltip": "Manuell als erledigt markiert"}
    if es.status == SyncStatus.SKIPPED:
        return {"target": target, "state": "grey", "tooltip": "Übersprungen"}
    if es.status == SyncStatus.FAILED:
        return {"target": target, "state": "red",
                "tooltip": es.last_error or "Letzter Sync fehlgeschlagen"}
    # pending / exported → local readiness decides
    st = sf.status_for_target(entry, project, target)
    if st["ready"]:
        return {"target": target, "state": "yellow", "tooltip": "Bereit zum Sync"}
    return {"target": target, "state": "red", "tooltip": "Fehlt: " + ", ".join(st["missing"])}


def matrix_row(entry, project) -> list[dict]:
    """One status cell per display target for an entry."""
    return [matrix_cell(entry, project, t) for t in DISPLAY_TARGETS]

