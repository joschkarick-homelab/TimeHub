"""Materialize per-target sync state (Phase 0).

Turns an entry's resolved target set (see services.sync_rules) into concrete
`EntrySync` rows, and reconciles them when targets change. Already-completed
rows (synced / manually_synced) are never removed, so audit trail and remote
references survive a re-evaluation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import EntrySync, TimeEntry
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


def reset_open_syncs_for_project(db: Session, project) -> int:
    """Re-open every not-yet-completed sync row of the project's entries.

    When a project is corrected — e.g. a previously invalid Salesforce
    assignment is fixed — its entries are often stuck on a stale ``failed``
    status from the earlier push attempt and the automatic sync skips them.
    Resetting those rows back to ``pending`` (and clearing the last error)
    lets the normal flow pick them up again. Completed rows
    (synced / manually_synced) carry a remote reference and are left intact.

    The legacy ``TimeEntry.sync_status`` mirror is reset too, since the
    Salesforce execute flow keys its idempotency check off it. Returns the
    number of entries that were touched. The caller commits.
    """
    entries = list(
        db.execute(
            select(TimeEntry)
            .where(TimeEntry.project_id == project.id)
            .options(selectinload(TimeEntry.entry_syncs))
        ).scalars()
    )
    touched = 0
    for entry in entries:
        changed = False
        for es in (entry.entry_syncs or []):
            if es.status not in _PROTECTED and es.status != SyncStatus.PENDING:
                es.status = SyncStatus.PENDING
                es.last_error = None
                es.synced_at = None
                db.add(es)
                changed = True
        if entry.sync_status not in _PROTECTED and entry.sync_status != SyncStatus.PENDING:
            entry.sync_status = SyncStatus.PENDING
            db.add(entry)
            changed = True
        if changed:
            touched += 1
    return touched


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
        es.synced_at = datetime.now(UTC)
        es.last_error = None
    db.add(es)
    return es


def mark_all_manually_synced(db: Session, entry) -> None:
    """Flag every not-yet-done target of an entry as manually handled."""
    for es in (entry.entry_syncs or []):
        if es.status not in _PROTECTED:
            es.status = SyncStatus.MANUALLY_SYNCED
            es.synced_at = datetime.now(UTC)
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


def _project_spec(project, target, fields) -> dict:
    """Form spec for the shared renderSyncFields JS: which fields to render,
    for which target, pre-filled from the project's stored metadata."""
    return {
        "target": target,
        "registry": {target: sf.fields_json(fields)},
        "current": project.sync_metadata or {},
    }


def _entry_spec(entry, target, fields) -> dict:
    return {
        "target": target,
        "registry": {target: sf.fields_json(fields)},
        "current": entry.sync_metadata_override or {},
    }


def wizard_buckets(entries, proj_lookup) -> dict[str, dict]:
    """Group the user's entries into per-target wizard buckets, driven by the
    materialized EntrySync rows.

    Per target:
      * `ready`            — pending + locally sync-ready (with total minutes)
      * `done`             — count of synced / manually marked
      * `project_gaps`     — projects missing required project-level fields
                             (e.g. the Salesforce assignment), deduplicated so
                             one inline form unblocks all the project's entries
      * `entry_gaps`       — entries missing required entry-level fields
      * `failed`           — last push failed (not inline-fixable; needs retry)
      * `blocked`          — count of distinct blocked entries
    Each gap carries a `spec` ready for the inline renderSyncFields form.
    """
    buckets: dict[str, dict] = {
        t: {
            "ready": [], "ready_minutes": 0, "done": 0,
            "project_gaps": {}, "entry_gaps": [], "failed": [], "_blocked": set(),
        }
        for t in DISPLAY_TARGETS
    }
    for e in entries:
        project = proj_lookup.get(e.project_id)
        if project is None:
            continue
        for row in (e.entry_syncs or []):
            t = row.target
            b = buckets.get(t)
            if b is None:
                continue
            if row.status in _PROTECTED:
                b["done"] += 1
                continue
            if row.status == SyncStatus.SKIPPED:
                continue
            if row.status == SyncStatus.FAILED:
                b["failed"].append(
                    {"entry": e, "reason": row.last_error or "Letzter Sync fehlgeschlagen"}
                )
                b["_blocked"].add(e.id)
                continue

            pf = sf.missing_project_fields(project, t)
            ef = sf.missing_entry_fields(e, project, t)
            if not pf and not ef:
                b["ready"].append(e)
                b["ready_minutes"] += e.duration_minutes
                continue

            b["_blocked"].add(e.id)
            if pf:
                gap = b["project_gaps"].get(project.id)
                if gap is None:
                    gap = {"project": project, "fields": list(pf), "entry_count": 0,
                           "spec": _project_spec(project, t, pf)}
                    b["project_gaps"][project.id] = gap
                gap["entry_count"] += 1
            if ef:
                b["entry_gaps"].append(
                    {"entry": e, "project": project, "fields": ef,
                     "spec": _entry_spec(e, t, ef)}
                )

    for b in buckets.values():
        b["project_gaps"] = list(b["project_gaps"].values())
        b["blocked"] = len(b.pop("_blocked"))
    return buckets


def mark_target_done(db: Session, entry, target: str) -> bool:
    """Flag one target of an entry as manually handled, if it isn't already
    done. Returns True when a change was applied."""
    row = _find_row(entry, target)
    if row is not None and row.status in _PROTECTED:
        return False
    set_target_status(db, entry, target, SyncStatus.MANUALLY_SYNCED)
    return True

