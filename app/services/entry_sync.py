"""Materialize per-target sync state (Phase 0).

Turns an entry's resolved target set (see services.sync_rules) into concrete
`EntrySync` rows, and reconciles them when targets change. Already-completed
rows (synced / manually_synced) are never removed, so audit trail and remote
references survive a re-evaluation.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import EntrySync
from app.models._enums import SyncStatus
from app.services.sync_rules import resolve_targets

# Rows in these states carry a remote reference / audit value and must not be
# deleted by reconciliation, even if the target no longer applies.
_PROTECTED = {SyncStatus.SYNCED, SyncStatus.MANUALLY_SYNCED}


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
