"""Shared, read-only resolution of TimeHub entries against Salesforce.

Both the sync *preview* (no DML) and the *execute* flow (which then POSTs the
pushable items) run the exact same checks — assignment lookup, Kontierungsmonat
lookup, status gating. Keeping that resolution here in one place means the
preview can't silently drift from what execute actually does.
"""

from app.services import salesforce as sf_svc
from app.services import sync_fields as sf


def _remote_value(entry, project):
    """Resolve the Remote__c flag via the sync-field registry (override →
    project default → field default)."""
    remote_field = next((f for f in sf.entry_fields("salesforce") if f.key == "remote"), None)
    if remote_field and project:
        return sf.entry_value(entry, project, remote_field, "salesforce")
    return None


def resolve_pushes(client, entries, proj_lookup) -> tuple[list[dict], str | None]:
    """Resolve each entry against Salesforce with cached assignment/period
    lookups.

    Returns ``(results, sf_error)``. Each result is a dict with ``entry`` and:
      * ``status == "pushable"`` → also ``assignment_id``, ``assignment``,
        ``period``, ``payload``, ``remote_value``. Fehlt der Kontierungsmonat,
        die Projektlaufzeit lässt ihn aber zu, ist ``period`` ein synthetischer
        Platzhalter (``id is None``, ``created=True``) und ``period_to_create``
        trägt die Monatsgrenzen — die Execute-Stufe legt den Monat dann an,
        bevor sie schreibt. ``payload`` ist in dem Fall bereits als Vorschau
        aufgebaut (``Kontierungsmonat__c is None``).
      * ``status == "blocked"``  → also ``reason``

    A :class:`SalesforceError` during an assignment/period lookup aborts the run
    and is returned as ``sf_error``; entries not yet processed are left out.
    """
    results: list[dict] = []
    assignments: dict[str, dict | None] = {}
    periods: dict[tuple[str, str], dict | None] = {}
    sf_error: str | None = None

    def block(entry, reason):
        results.append({"entry": entry, "status": "blocked", "reason": reason})

    for e in entries:
        project = proj_lookup.get(e.project_id)
        aid = sf_svc.assignment_id_for(e, project) if project else None
        if not aid:
            block(e, "keine Projektbesetzung gepflegt")
            continue

        if aid not in assignments:
            try:
                assignments[aid] = sf_svc.get_assignment(client, aid)
            except sf_svc.SalesforceError as err:
                sf_error = str(err)
                break
        assignment = assignments[aid]
        if assignment is None:
            block(e, "Projektbesetzung nicht in SF gefunden")
            continue
        if assignment.get("closed"):
            block(e, "Projektbesetzung in SF geschlossen")
            continue

        key = (aid, e.entry_date.strftime("%Y-%m"))
        if key not in periods:
            try:
                periods[key] = sf_svc.get_monthly_period(client, aid, e.entry_date.isoformat())
            except sf_svc.SalesforceError as err:
                sf_error = str(err)
                break
        period = periods[key]
        month_label = e.entry_date.strftime("%m/%Y")
        if period is None:
            # Kein Kontierungsmonat vorhanden — automatisch anlegen, sofern die
            # Projektlaufzeit der Projektbesetzung den Tag abdeckt. Angelegt wird
            # erst in der Execute-Stufe (resolve bleibt read-only); hier wird der
            # Push nur als „mit Neuanlage“ vorgemerkt.
            bounds = sf_svc.period_creation_bounds(assignment, e.entry_date)
            if bounds is None:
                block(e, f"Kein Kontierungsmonat {month_label} für diese "
                         f"Projektbesetzung und Projektlaufzeit lässt keinen zu")
                continue
            remote_value = _remote_value(e, project)
            results.append({
                "entry": e,
                "status": "pushable",
                "assignment_id": aid,
                "assignment": assignment,
                "period": {
                    "id": None,
                    "name": month_label,
                    "start_date": bounds[0].isoformat(),
                    "end_date": bounds[1].isoformat(),
                    "status": "offen",
                    "closed": False,
                    "created": True,
                },
                "period_to_create": {"start": bounds[0], "end": bounds[1]},
                # Vorschau-Payload ohne Kontierungsmonat-Id; die echte Id wird
                # nach der Neuanlage in der Execute-Stufe eingesetzt.
                "payload": sf_svc.build_zeiterfassung_payload(e, None, remote_value),
                "remote_value": remote_value,
            })
            continue
        name = period.get("name") or month_label
        if period.get("closed"):
            block(e, f"Kontierungsmonat {name} ist abgeschlossen")
            continue
        pstatus = (period.get("status") or "").strip()
        if pstatus.lower() != "offen":
            block(e, f"Kontierungsmonat {name} ist nicht offen (Status: {pstatus or '—'})")
            continue

        remote_value = _remote_value(e, project)
        results.append({
            "entry": e,
            "status": "pushable",
            "assignment_id": aid,
            "assignment": assignment,
            "period": period,
            "period_to_create": None,
            "payload": sf_svc.build_zeiterfassung_payload(e, period["id"], remote_value),
            "remote_value": remote_value,
        })

    return results, sf_error
