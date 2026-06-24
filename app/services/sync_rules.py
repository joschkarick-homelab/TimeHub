"""Target resolution (Phase 0): derive an entry's effective target *set*.

Base is the project's default target set (`Project.sync_targets`, falling back
to the single `default_sync_target` for back-compat). On top of that, a small
set of declarative `SyncRule`s may add, remove or replace targets based on the
entry's attributes.

Resolution order:
  1. An explicit per-entry override (`sync_targets_override`, or the legacy
     single `sync_target_override`) wins entirely — rules are skipped so manual
     edits are never clobbered.
  2. Otherwise: project default set, then applicable rules by priority.

Non-sync targets (intern/none) are always dropped from the result.
"""

from __future__ import annotations

from app.services.sync_fields import NON_SYNC_TARGETS, project_targets


def _condition_matches(cond: dict | None, entry, project) -> bool:
    """Tiny, extensible condition vocabulary. Unknown types never match."""
    cond = cond or {}
    ctype = cond.get("type") or "always"
    values = cond.get("values") or []
    if ctype == "always":
        return True
    if ctype == "has_tag":
        return any(t in values for t in (getattr(entry, "tags", None) or []))
    if ctype == "project_code":
        return getattr(project, "code", None) in values
    return False


def _applicable(rules, project, entry):
    """Enabled rules in evaluation order: priority asc, global before project."""
    def key(r):
        return (r.priority, 0 if r.scope == "global" else 1, r.id or 0)

    for rule in sorted(rules, key=key):
        if not rule.enabled:
            continue
        if rule.scope == "project" and rule.project_id != project.id:
            continue
        if not _condition_matches(rule.condition, entry, project):
            continue
        yield rule


def _apply(rule, targets: set[str]) -> None:
    if rule.action == "add_target" and rule.target:
        targets.add(rule.target)
    elif rule.action == "remove_target" and rule.target:
        targets.discard(rule.target)
    elif rule.action == "set_targets":
        targets.clear()
        targets.update(rule.targets or [])


def resolve_targets(project, entry, rules=()) -> list[str]:
    """Effective, deduplicated, sorted target set for an entry."""
    override = getattr(entry, "sync_targets_override", None)
    if override:
        base = set(override)
    elif getattr(entry, "sync_target_override", None):
        base = {entry.sync_target_override}
    else:
        base = set(project_targets(project))
        for rule in _applicable(rules, project, entry):
            _apply(rule, base)
    return sorted(base - NON_SYNC_TARGETS)


def load_rules(db):
    """All enabled rules, ready to pass to `resolve_targets`."""
    from app.models import SyncRule

    return list(db.query(SyncRule).filter(SyncRule.enabled.is_(True)).all())
