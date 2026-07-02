"""The shared Salesforce push resolver (M3a) — preview and execute both rely on
it, so its pushable/blocked decisions must stay stable."""

from datetime import date
from types import SimpleNamespace


def _entry(eid=1, pid=1):
    return SimpleNamespace(id=eid, project_id=pid, entry_date=date(2026, 5, 27),
                           start_time=None, end_time=None, duration_minutes=90,
                           description="x", sync_metadata_override={},
                           sync_target_override=None)


def _patch(monkeypatch, *, assignment, period):
    from app.services import sf_push
    monkeypatch.setattr(sf_push.sf_svc, "assignment_id_for", lambda e, p: "a01000000000001")
    monkeypatch.setattr(sf_push.sf_svc, "get_assignment", lambda _c, aid: assignment)
    monkeypatch.setattr(sf_push.sf_svc, "get_monthly_period", lambda _c, _a, _d: period)
    monkeypatch.setattr(sf_push.sf_svc, "build_zeiterfassung_payload",
                        lambda e, pid, rv: {"Kontierungsmonat__c": pid})
    monkeypatch.setattr(sf_push, "_remote_value", lambda e, p: None)
    # period_creation_bounds keeps its real implementation so the runtime gate is
    # exercised end-to-end from the assignment's project_start/project_end.


def test_resolve_pushable_when_period_open(monkeypatch):
    from app.services import sf_push
    _patch(monkeypatch,
           assignment={"id": "a01000000000001", "closed": False},
           period={"id": "a0Q", "name": "05/2026", "status": "offen", "closed": False})
    e = _entry()
    results, sf_error = sf_push.resolve_pushes(None, [e], {1: SimpleNamespace(id=1)})
    assert sf_error is None
    assert results[0]["status"] == "pushable"
    assert results[0]["payload"]["Kontierungsmonat__c"] == "a0Q"


def test_resolve_blocks_when_period_not_open(monkeypatch):
    from app.services import sf_push
    _patch(monkeypatch,
           assignment={"id": "a01000000000001", "closed": False},
           period={"id": "a0Q", "name": "05/2026", "status": "in Bearbeitung", "closed": False})
    results, _ = sf_push.resolve_pushes(None, [_entry()], {1: SimpleNamespace(id=1)})
    assert results[0]["status"] == "blocked"
    assert "nicht offen" in results[0]["reason"]


def test_resolve_blocks_when_assignment_missing(monkeypatch):
    from app.services import sf_push
    _patch(monkeypatch, assignment=None, period=None)
    results, _ = sf_push.resolve_pushes(None, [_entry()], {1: SimpleNamespace(id=1)})
    assert results[0]["status"] == "blocked"
    assert "nicht in SF gefunden" in results[0]["reason"]


def test_resolve_auto_creates_period_when_runtime_allows(monkeypatch):
    """Kein Kontierungsmonat, aber die Projektlaufzeit deckt den Tag ab → der
    Push bleibt pushable und wird als 'Monat anlegen' vorgemerkt."""
    from app.services import sf_push
    _patch(monkeypatch,
           assignment={"id": "a01000000000001", "closed": False,
                       "project_start": "2026-01-01", "project_end": "2026-12-31"},
           period=None)
    results, sf_error = sf_push.resolve_pushes(None, [_entry()], {1: SimpleNamespace(id=1)})
    assert sf_error is None
    r = results[0]
    assert r["status"] == "pushable"
    assert r["period"]["id"] is None
    assert r["period"]["created"] is True
    # Voller Kalendermonat des Eintragsdatums (2026-05-27).
    assert r["period_to_create"]["start"] == date(2026, 5, 1)
    assert r["period_to_create"]["end"] == date(2026, 5, 31)
    # Vorschau-Payload ohne Kontierungsmonat-Id (wird erst beim Anlegen gesetzt).
    assert r["payload"]["Kontierungsmonat__c"] is None


def test_resolve_auto_creates_period_when_runtime_dates_empty(monkeypatch):
    """Leere Projektlaufzeit ist permissiv — Monat wird trotzdem vorgemerkt."""
    from app.services import sf_push
    _patch(monkeypatch,
           assignment={"id": "a01000000000001", "closed": False,
                       "project_start": None, "project_end": None},
           period=None)
    results, _ = sf_push.resolve_pushes(None, [_entry()], {1: SimpleNamespace(id=1)})
    assert results[0]["status"] == "pushable"
    assert results[0]["period_to_create"]["start"] == date(2026, 5, 1)


def test_resolve_blocks_when_runtime_excludes_day(monkeypatch):
    """Kein Kontierungsmonat UND der Tag liegt außerhalb der Projektlaufzeit →
    blockiert (kein Monat wird angelegt)."""
    from app.services import sf_push
    _patch(monkeypatch,
           assignment={"id": "a01000000000001", "closed": False,
                       "project_start": "2026-01-01", "project_end": "2026-04-30"},
           period=None)
    results, _ = sf_push.resolve_pushes(None, [_entry()], {1: SimpleNamespace(id=1)})
    assert results[0]["status"] == "blocked"
    assert "Projektlaufzeit" in results[0]["reason"]


def test_resolve_aborts_on_salesforce_error(monkeypatch):
    from app.services import sf_push

    def boom(_c, _aid):
        raise sf_push.sf_svc.SalesforceError("Salesforce nicht erreichbar")

    monkeypatch.setattr(sf_push.sf_svc, "assignment_id_for", lambda e, p: "a01000000000001")
    monkeypatch.setattr(sf_push.sf_svc, "get_assignment", boom)
    results, sf_error = sf_push.resolve_pushes(None, [_entry()], {1: SimpleNamespace(id=1)})
    assert results == []
    assert "nicht erreichbar" in sf_error
