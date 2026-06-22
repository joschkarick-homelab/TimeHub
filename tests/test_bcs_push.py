"""BCS push: the pure SOAP-shaping helpers and the grouping resolver
(preview + execute both rely on the latter, so its grouping/blocking decisions
must stay stable). No live BCS call is made — the client is stubbed."""

from datetime import date
from types import SimpleNamespace


def _entry(eid=1, pid=1, day=27, minutes=90, desc="x", wp="WP1"):
    return SimpleNamespace(
        id=eid, project_id=pid, entry_date=date(2026, 5, day),
        duration_minutes=minutes, description=desc,
        sync_metadata_override={"bcs": {"work_package": wp}} if wp else {},
    )


def _project(pid=1, default_wp=None):
    md = {"bcs": {"default_work_package": default_wp}} if default_wp else {}
    return SimpleNamespace(id=pid, sync_metadata=md)


def _user(uid=7, email="berater@firma.de"):
    return SimpleNamespace(id=uid, email=email)


class _Client:
    """Stub BCS client: returns a fixed bookable work-package list, or raises."""

    def __init__(self, options=None, error=None):
        self._options = options if options is not None else [{"value": "WP1", "label": "Analyse (Projekt X)"}]
        self._error = error
        self.calls = []

    def list_work_packages(self, email, date_iso):
        self.calls.append((email, date_iso))
        if self._error:
            from app.services.bcs import BcsError
            raise BcsError(self._error)
        return self._options


# ---------- pure helpers ----------


def test_build_time_record_args_shape():
    from app.services.bcs import EXTERNAL_SYSTEM_NAME, build_time_record_args
    args = build_time_record_args(external_id="7:2026-05-27:WP1", work_package_oid="WP1",
                                  date_iso="2026-05-27", expense_minutes=120,
                                  comment="Analyse", employee_login="a@b.de")
    assert args["id"] == {"_value_1": "7:2026-05-27:WP1", "systemName": EXTERNAL_SYSTEM_NAME}
    assert args["target"] == {"task": {"bcsOid": "WP1"}}
    assert args["date"] == "2026-05-27"
    assert args["expense"] == 120
    assert args["comment"] == "Analyse"
    assert args["employee"] == {"login": "a@b.de"}


def test_work_packages_from_timesheet_parses_attributes():
    from app.services.bcs import work_packages_from_timesheet
    task = SimpleNamespace(
        bcsOid="WP1", name="Analyse",
        timesheetEntryProperties=SimpleNamespace(project=SimpleNamespace(name="Projekt X")),
    )
    resp = SimpleNamespace(timesheetEntries=SimpleNamespace(task=[task]))
    out = work_packages_from_timesheet(resp)
    assert out == [{"value": "WP1", "label": "Analyse (Projekt X)",
                    "search": "analyse projekt x"}]


def test_oid_from_response():
    from app.services.bcs import oid_from_response
    assert oid_from_response(SimpleNamespace(oid="rec-42")) == "rec-42"
    assert oid_from_response(SimpleNamespace(oid=None)) is None


def test_work_package_oid_for_entry_overrides_project():
    from app.services.bcs import work_package_oid_for
    assert work_package_oid_for(_entry(wp="WP-E"), _project(default_wp="WP-P")) == "WP-E"
    assert work_package_oid_for(_entry(wp=None), _project(default_wp="WP-P")) == "WP-P"
    assert work_package_oid_for(_entry(wp=None), _project()) is None


# ---------- resolver ----------


def test_resolve_groups_same_day_and_wp():
    from app.services import bcs_push
    e1 = _entry(eid=1, minutes=60, desc="A")
    e2 = _entry(eid=2, minutes=30, desc="B")
    client = _Client()
    results, err = bcs_push.resolve_pushes(client, [e1, e2], {1: _project()}, _user())
    assert err is None
    assert len(results) == 1
    g = results[0]
    assert g["status"] == "pushable"
    assert g["total_minutes"] == 90
    assert g["comment"] == "A; B"
    assert g["external_id"] == "7:2026-05-27:WP1"
    assert g["args"]["expense"] == 90


def test_resolve_separates_different_days():
    from app.services import bcs_push
    results, _ = bcs_push.resolve_pushes(
        _Client(), [_entry(eid=1, day=27), _entry(eid=2, day=28)], {1: _project()}, _user()
    )
    assert len(results) == 2
    assert {r["date"] for r in results} == {"2026-05-27", "2026-05-28"}


def test_resolve_blocks_entry_without_work_package():
    from app.services import bcs_push
    results, _ = bcs_push.resolve_pushes(_Client(), [_entry(wp=None)], {1: _project()}, _user())
    assert results[0]["status"] == "blocked"
    assert "kein Arbeitspaket" in results[0]["reason"]


def test_resolve_blocks_when_not_bookable():
    from app.services import bcs_push
    # client returns an empty bookable list → WP1 is not bookable that day.
    results, _ = bcs_push.resolve_pushes(_Client(options=[]), [_entry()], {1: _project()}, _user())
    assert results[0]["status"] == "blocked"
    assert "nicht buchbar" in results[0]["reason"]


def test_resolve_aborts_on_bcs_error():
    from app.services import bcs_push
    results, err = bcs_push.resolve_pushes(
        _Client(error="BCS nicht erreichbar"), [_entry()], {1: _project()}, _user()
    )
    assert results == []
    assert "nicht erreichbar" in err
