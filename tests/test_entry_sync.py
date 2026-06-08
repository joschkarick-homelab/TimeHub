"""Phase 0: multi-target resolution, status per target, and materialization."""

from types import SimpleNamespace

from app.services import entry_sync as es
from app.services import sync_fields as sf
from app.services.sync_rules import resolve_targets


# ---------- pure resolution logic ----------

def _proj(targets=None, default="intern", code="ABC"):
    return SimpleNamespace(
        id=1, code=code, sync_targets=targets or [], default_sync_target=default, sync_metadata={}
    )


def _ent(targets_override=None, single_override=None, tags=None, metadata=None):
    return SimpleNamespace(
        sync_targets_override=targets_override,
        sync_target_override=single_override,
        tags=tags or [],
        sync_metadata_override=metadata or {},
    )


def _rule(action, target=None, targets=None, condition=None, scope="global", project_id=None,
          priority=100, enabled=True, _id=1):
    return SimpleNamespace(
        id=_id, action=action, target=target, targets=targets, condition=condition or {},
        scope=scope, project_id=project_id, priority=priority, enabled=enabled,
    )


def test_project_targets_falls_back_to_single_default():
    assert sf.project_targets(_proj(default="salesforce")) == ["salesforce"]
    assert sf.project_targets(_proj(targets=["jira", "bcs"])) == ["jira", "bcs"]
    # intern/none are never real targets
    assert sf.project_targets(_proj(default="intern")) == []


def test_resolve_uses_project_default_set():
    p = _proj(targets=["jira", "bcs"])
    assert resolve_targets(p, _ent()) == ["bcs", "jira"]


def test_resolve_override_wins_over_project_and_rules():
    p = _proj(targets=["jira", "bcs"])
    rules = [_rule("add_target", target="salesforce", condition={"type": "always"})]
    assert resolve_targets(p, _ent(targets_override=["salesforce"]), rules) == ["salesforce"]


def test_resolve_legacy_single_override_still_works():
    p = _proj(targets=["jira"])
    assert resolve_targets(p, _ent(single_override="bcs")) == ["bcs"]


def test_rule_add_remove_and_non_sync_dropped():
    p = _proj(targets=["jira"])
    add = _rule("add_target", target="salesforce", condition={"type": "has_tag", "values": ["billable"]})
    assert resolve_targets(p, _ent(tags=["billable"]), [add]) == ["jira", "salesforce"]
    # tag absent -> rule does not fire
    assert resolve_targets(p, _ent(tags=["intern"]), [add]) == ["jira"]
    rm = _rule("remove_target", target="jira", condition={"type": "always"})
    assert resolve_targets(p, _ent(), [rm]) == []


def test_rule_project_code_condition_and_set_targets():
    p = _proj(targets=["jira"], code="ACME")
    rule = _rule("set_targets", targets=["bcs", "salesforce", "none"],
                 condition={"type": "project_code", "values": ["ACME"]})
    assert resolve_targets(p, _ent(), [rule]) == ["bcs", "salesforce"]
    # different project code -> rule skipped, project default stands
    assert resolve_targets(_proj(targets=["jira"], code="OTHER"), _ent(), [rule]) == ["jira"]


def test_disabled_and_project_scoped_rules():
    p = _proj(targets=["jira"], code="ACME")
    disabled = _rule("add_target", target="bcs", condition={"type": "always"}, enabled=False)
    assert resolve_targets(p, _ent(), [disabled]) == ["jira"]
    scoped = _rule("add_target", target="bcs", condition={"type": "always"},
                   scope="project", project_id=999)
    assert resolve_targets(p, _ent(), [scoped]) == ["jira"]  # wrong project


def test_entry_sync_statuses_reports_missing_per_target():
    # salesforce needs assignment_id at project level; bcs needs entry subject/task
    p = _proj(targets=["salesforce", "bcs"])
    statuses = sf.entry_sync_statuses(_ent(), p, ["salesforce", "bcs"])
    assert statuses["salesforce"]["ready"] is False
    assert statuses["bcs"]["ready"] is False
    assert "BCS Subject" in statuses["bcs"]["missing"]


# ---------- status-matrix cell logic ----------

def _es(target, status="pending", last_error=None):
    return SimpleNamespace(target=target, status=status, last_error=last_error)


def _ent_syncs(syncs, metadata=None):
    return SimpleNamespace(
        entry_syncs=syncs, sync_metadata_override=metadata or {},
        sync_targets_override=None, sync_target_override=None, tags=[],
    )


def test_matrix_cell_grey_when_target_not_applicable():
    cell = es.matrix_cell(_ent_syncs([]), _proj(), "jira")
    assert cell["state"] == "grey"


def test_matrix_cell_green_for_synced_and_manual():
    assert es.matrix_cell(_ent_syncs([_es("salesforce", "synced")]), _proj(), "salesforce")["state"] == "green"
    assert es.matrix_cell(_ent_syncs([_es("bcs", "manually_synced")]), _proj(), "bcs")["state"] == "green"


def test_matrix_cell_grey_for_skipped():
    assert es.matrix_cell(_ent_syncs([_es("jira", "skipped")]), _proj(), "jira")["state"] == "grey"


def test_matrix_cell_red_failed_surfaces_error():
    cell = es.matrix_cell(_ent_syncs([_es("salesforce", "failed", last_error="boom")]), _proj(), "salesforce")
    assert cell["state"] == "red" and "boom" in cell["tooltip"]


def test_matrix_cell_red_when_pending_but_blocked():
    # salesforce needs a project assignment_id that _proj() lacks
    cell = es.matrix_cell(_ent_syncs([_es("salesforce", "pending")]), _proj(), "salesforce")
    assert cell["state"] == "red"


def test_matrix_cell_yellow_when_pending_and_ready():
    e = _ent_syncs([_es("jira", "pending")], metadata={"jira": {"issue_key": "ABC-1"}})
    assert es.matrix_cell(e, _proj(), "jira")["state"] == "yellow"


# ---------- wizard buckets ----------

def _full_ent(eid, pid, minutes, syncs, metadata=None):
    return SimpleNamespace(
        id=eid, project_id=pid, duration_minutes=minutes, entry_syncs=syncs,
        sync_metadata_override=metadata or {}, sync_targets_override=None,
        sync_target_override=None, tags=[],
    )


def test_wizard_buckets_groups_ready_blocked_done():
    p = _proj(targets=["jira", "bcs"])  # id=1
    proj_lookup = {1: p}
    e1 = _full_ent(1, 1, 120, [_es("jira", "pending"), _es("bcs", "pending")],
                   metadata={"jira": {"issue_key": "ABC-1"}})
    e2 = _full_ent(2, 1, 60, [_es("jira", "synced")])
    e3 = _full_ent(3, 1, 30, [_es("jira", "failed", last_error="kaputt")])
    b = es.wizard_buckets([e1, e2, e3], proj_lookup)
    assert [x.id for x in b["jira"]["ready"]] == [1]
    assert b["jira"]["ready_minutes"] == 120
    assert b["jira"]["done"] == 1
    assert len(b["jira"]["blocked"]) == 1 and "kaputt" in b["jira"]["blocked"][0]["reason"]
    # bcs: entry 1 is blocked (subject/task missing)
    assert len(b["bcs"]["blocked"]) == 1
    assert b["bcs"]["ready"] == []


# ---------- materialization through the real API ----------

def _token(client) -> str:
    return client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]


def _h(client):
    return {"Authorization": f"Bearer {_token(client)}"}


def _project_id(client, code, target="salesforce"):
    r = client.post(
        "/api/v1/projects",
        json={"name": code.title(), "code": code, "default_sync_target": target},
        headers=_h(client),
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    return next(
        p["id"] for p in client.get("/api/v1/projects", headers=_h(client)).json()
        if p["code"] == code
    )


def _entry_syncs(entry_id):
    from app.db import SessionLocal
    from app.models import EntrySync

    with SessionLocal() as db:
        rows = db.query(EntrySync).filter(EntrySync.entry_id == entry_id).all()
        return {r.target: r.status for r in rows}


def test_api_create_materializes_one_row(client):
    pid = _project_id(client, "MATONE", target="salesforce")
    r = client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-28", "duration_minutes": 60},
        headers=_h(client),
    )
    assert r.status_code == 201
    assert _entry_syncs(r.json()["id"]) == {"salesforce": "pending"}


def test_api_create_intern_project_has_no_rows(client):
    pid = _project_id(client, "MATINT", target="intern")
    r = client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-28", "duration_minutes": 60},
        headers=_h(client),
    )
    assert _entry_syncs(r.json()["id"]) == {}


def test_multi_target_project_materializes_each_target(client):
    pid = _project_id(client, "MATMULTI", target="jira")
    # widen the project to a multi-target set directly (no API surface yet)
    from app.db import SessionLocal
    from app.models import Project

    with SessionLocal() as db:
        p = db.get(Project, pid)
        p.sync_targets = ["jira", "bcs"]
        db.add(p)
        db.commit()

    r = client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-28", "duration_minutes": 60},
        headers=_h(client),
    )
    assert _entry_syncs(r.json()["id"]) == {"jira": "pending", "bcs": "pending"}


# ---------- dashboard rendering + manual-mark bridge (web) ----------

def _web_login(client):
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def test_dashboard_renders_matrix_columns(client):
    _web_login(client)
    pid = _project_id(client, "DASHMAT", target="salesforce")
    client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-28", "duration_minutes": 60},
        headers=_h(client),
    )
    r = client.get("/?date_from=2026-05-01&date_to=2026-05-31")
    assert r.status_code == 200
    for label in ("Jira", "BCS", "Salesforce"):
        assert label in r.text


def test_mark_synced_flips_matrix_to_green(client):
    _web_login(client)
    pid = _project_id(client, "MARKGRN", target="salesforce")
    eid = client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-28", "duration_minutes": 60},
        headers=_h(client),
    ).json()["id"]
    assert _entry_syncs(eid) == {"salesforce": "pending"}

    r = client.post("/entries/mark-synced", data={"entry_ids": eid}, follow_redirects=False)
    assert r.status_code == 302
    assert _entry_syncs(eid) == {"salesforce": "manually_synced"}

    # undo restores pending
    client.post(f"/entries/{eid}/unmark-synced", follow_redirects=False)
    assert _entry_syncs(eid) == {"salesforce": "pending"}


# ---------- export wizard (web) ----------

def _jira_ready_entry(client, code):
    pid = _project_id(client, code, target="jira")
    return client.post(
        "/api/v1/time-entries",
        json={"project_id": pid, "entry_date": "2026-05-28", "duration_minutes": 60,
              "sync_metadata_override": {"jira": {"issue_key": "ABC-1"}}},
        headers=_h(client),
    ).json()["id"]


def test_wizard_renders_target_cards(client):
    _web_login(client)
    _jira_ready_entry(client, "WIZJIRA")
    r = client.get("/sync")
    assert r.status_code == 200
    assert "Export-Wizard" in r.text
    for label in ("Jira", "BCS", "Salesforce"):
        assert label in r.text
    assert "Als erledigt markieren" in r.text  # jira entry is ready


def test_wizard_mark_done_sets_target_manually_synced(client):
    _web_login(client)
    eid = _jira_ready_entry(client, "WIZMARK")
    assert _entry_syncs(eid) == {"jira": "pending"}
    r = client.post("/sync/jira/mark-done", data={"entry_ids": eid}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/sync")
    assert _entry_syncs(eid) == {"jira": "manually_synced"}


def test_wizard_unknown_target_rejected(client):
    _web_login(client)
    r = client.post("/sync/bogus/mark-done", data={"entry_ids": 1}, follow_redirects=False)
    assert r.status_code == 302 and "error" in r.headers["location"]


def test_edit_next_redirects_back_to_wizard(client):
    _web_login(client)
    eid = _jira_ready_entry(client, "WIZEDIT")
    r = client.post(
        f"/entries/{eid}/edit",
        data={"entry_date": "2026-05-28", "project_id": _project_id(client, "WIZEDIT", "jira"),
              "duration_minutes": 90, "next": "/sync"},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == "/sync"
