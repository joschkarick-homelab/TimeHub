"""Shared, read-only resolution of TimeHub entries against BCS.

Unlike the Salesforce push (1 entry → 1 record), BCS aggregates **one booking
per (user, date, work package)** — so this resolver *groups* the entries first,
sums their durations and merges their descriptions, then validates the work
package against the user's currently bookable packages (via ``GetTimesheet``).

Both the preview (no write) and the execute flow use this, so the preview can't
drift from what execute does.

Idempotency / known limitation: each group's BCS record is keyed by a stable
``external_id`` (user+date+work package). A re-push of the *same* selection is a
no-op upsert. But because BCS upserts on that key, pushing only *part* of a
day's entries for one work package and later pushing the rest would **overwrite**
(not add to) the first booking. Until the live test, the recommendation is to
sync a day's entries for a work package together. (See docs/bcs-integration.md.)
"""

from __future__ import annotations

from app.services import bcs as bcs_svc


def _merge_comments(entries) -> str:
    """Join distinct, non-empty descriptions in order."""
    seen = dict.fromkeys(
        e.description.strip() for e in entries if e.description and e.description.strip()
    )
    return "; ".join(seen)


def resolve_pushes(client, entries, proj_lookup, user) -> tuple[list[dict], str | None]:
    """Group ``entries`` (all belonging to ``user``) into BCS bookings.

    Returns ``(results, bcs_error)``. Each result is a dict with ``entries`` and:
      * ``status == "pushable"`` → also ``date``, ``work_package_oid``,
        ``work_package_label``, ``total_minutes``, ``comment``, ``external_id``,
        ``args`` (the ready ``CreateOrUpdateTimeRecord`` kwargs).
      * ``status == "blocked"``  → also ``reason``.

    A :class:`bcs.BcsError` during a ``GetTimesheet`` lookup aborts the run and
    is returned as ``bcs_error``; groups not yet processed are left out.
    """
    results: list[dict] = []
    # Bucket by (date, work package), preserving first-seen order. Entries
    # without a work package are blocked individually.
    groups: dict[tuple[str, str], list] = {}
    for e in entries:
        project = proj_lookup.get(e.project_id)
        wp = bcs_svc.work_package_oid_for(e, project) if project else None
        if not wp:
            results.append({"status": "blocked", "entries": [e],
                            "reason": "kein Arbeitspaket gepflegt"})
            continue
        groups.setdefault((e.entry_date.isoformat(), wp), []).append(e)

    # Validate each group's work package against the bookable list for that date.
    wp_by_date: dict[str, dict] = {}
    bcs_error: str | None = None
    for (date_iso, wp), grouped in groups.items():
        if date_iso not in wp_by_date:
            try:
                options = client.list_work_packages(user.email, date_iso)
            except bcs_svc.BcsError as err:
                bcs_error = str(err)
                break
            wp_by_date[date_iso] = {o["value"]: o for o in options}

        option = wp_by_date[date_iso].get(wp)
        total = sum(e.duration_minutes for e in grouped)
        comment = _merge_comments(grouped)
        external_id = f"{user.id}:{date_iso}:{wp}"
        base = {
            "entries": grouped,
            "date": date_iso,
            "work_package_oid": wp,
            "work_package_label": option["label"] if option else wp,
            "total_minutes": total,
            "comment": comment,
            "external_id": external_id,
        }
        if option is None:
            results.append({**base, "status": "blocked",
                            "reason": "Arbeitspaket an diesem Tag nicht buchbar"})
            continue
        base["args"] = bcs_svc.build_time_record_args(
            external_id=external_id,
            work_package_oid=wp,
            date_iso=date_iso,
            expense_minutes=total,
            comment=comment,
            employee_login=user.email,
        )
        results.append({**base, "status": "pushable"})

    return results, bcs_error
