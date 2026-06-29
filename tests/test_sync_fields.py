"""Target-dependent sync fields (Phase 1): registry logic + web wiring."""

from types import SimpleNamespace


def _login_session(client) -> None:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")


def _token(client) -> str:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")
    return "hub-identity"


def _project(client, code: str, target: str, metadata: dict | None = None) -> int:
    h = {"Authorization": f"Bearer {_token(client)}"}
    r = client.post(
        "/api/v1/projects",
        json={"name": code.title(), "code": code, "default_sync_target": target,
              "sync_metadata": metadata or {}},
        headers=h,
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    return next(
        p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == code
    )


def _entry(client, project_id: int, **extra) -> dict:
    h = {"Authorization": f"Bearer {_token(client)}"}
    payload = {"project_id": project_id, "entry_date": "2026-05-28", "duration_minutes": 60}
    payload.update(extra)
    return client.post("/api/v1/time-entries", json=payload, headers=h).json()


# ---------- registry / service logic ----------

def _proj(target, metadata=None):
    return SimpleNamespace(default_sync_target=target, sync_metadata=metadata or {})


def _ent(override=None, metadata=None):
    return SimpleNamespace(sync_target_override=override, sync_metadata_override=metadata or {})


def test_effective_target_override_wins():
    from app.services import sync_fields as sf
    p = _proj("salesforce")
    assert sf.effective_target(_ent(), p) == "salesforce"
    assert sf.effective_target(_ent(override="jira"), p) == "jira"


def test_validate_value_pattern():
    from app.services import sync_fields as sf
    field = sf.entry_fields("jira")[0]  # issue_key, pattern [A-Z]+-\d+
    assert sf.validate_value(field, "ABC-123") is None
    assert sf.validate_value(field, "nonsense") is not None
    assert sf.validate_value(field, "") is None  # empty is not a format error


def test_entry_status_missing_then_inherited():
    from app.services import sync_fields as sf
    # jira project without a default issue → entry without ticket is not ready
    p = _proj("jira")
    st = sf.entry_sync_status(_ent(), p)
    assert st["needs_sync"] and not st["ready"]
    assert "Jira-Ticket" in st["missing"]
    # entry carries its own ticket → ready
    st2 = sf.entry_sync_status(_ent(metadata={"jira": {"issue_key": "ABC-9"}}), p)
    assert st2["ready"]
    # project default issue is inherited when the entry has none
    p2 = _proj("jira", {"jira": {"default_issue": "ABC-1"}})
    assert sf.entry_sync_status(_ent(), p2)["ready"]


def test_bcs_entry_needs_subject_and_task():
    from app.services import sync_fields as sf
    p = _proj("bcs")
    st = sf.entry_sync_status(_ent(), p)
    assert st["needs_sync"] and not st["ready"]
    assert "BCS Subject" in st["missing"] and "BCS Task" in st["missing"]
    ready = sf.entry_sync_status(_ent(metadata={"bcs": {"subject": "Support", "task": "Analyse"}}), p)
    assert ready["ready"]


def test_entry_status_intern_always_ready():
    from app.services import sync_fields as sf
    st = sf.entry_sync_status(_ent(), _proj("intern"))
    assert st["ready"] and not st["needs_sync"]


def test_project_status_salesforce_requires_assignment_id():
    from app.services import sync_fields as sf
    assert not sf.project_sync_status(_proj("salesforce"))["ready"]
    # 15-char Salesforce ID
    ok = sf.project_sync_status(_proj("salesforce", {"salesforce": {"assignment_id": "a01000000000001"}}))
    assert ok["ready"]
    # too short → format violation, not ready
    bad = sf.project_sync_status(_proj("salesforce", {"salesforce": {"assignment_id": "short"}}))
    assert not bad["ready"]


def test_apply_fields_sets_and_clears():
    from app.services import sync_fields as sf
    fields = sf.entry_fields("jira")
    md, warn = sf.apply_fields({}, "jira", fields, {"issue_key": "ABC-1"})
    assert md == {"jira": {"issue_key": "ABC-1"}} and not warn
    # malformed value is stored but warns
    md2, warn2 = sf.apply_fields({}, "jira", fields, {"issue_key": "bad"})
    assert warn2 and md2["jira"]["issue_key"] == "bad"
    # empty clears the key (and the now-empty target)
    md3, _ = sf.apply_fields(md, "jira", fields, {"issue_key": ""})
    assert "jira" not in md3


# ---------- web wiring ----------

def test_project_create_stores_salesforce_id(client):
    _login_session(client)
    client.post("/projects", data={
        "name": "SF Proj", "code": "SFWEB", "default_sync_target": "salesforce",
        "status": "active", "meta__salesforce__assignment_id": "a01000000000999",
    }, follow_redirects=False)
    h = {"Authorization": f"Bearer {_token(client)}"}
    p = next(p for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == "SFWEB")
    assert p["sync_metadata"] == {"salesforce": {"assignment_id": "a01000000000999"}}


def test_entry_edit_stores_jira_ticket_and_override(client):
    _login_session(client)
    pid = _project(client, "JIRAEDIT", "jira")
    eid = _entry(client, pid)["id"]
    r = client.post(f"/entries/{eid}/edit", data={
        "entry_date": "2026-05-28", "project_id": str(pid), "duration_minutes": "60",
        "sync_target_override": "jira", "meta__jira__issue_key": "ABC-123",
    }, follow_redirects=False)
    assert r.status_code == 302
    h = {"Authorization": f"Bearer {_token(client)}"}
    e = client.get(f"/api/v1/time-entries/{eid}", headers=h).json()
    assert e["sync_metadata_override"] == {"jira": {"issue_key": "ABC-123"}}


def test_dashboard_create_with_meta(client):
    _login_session(client)
    pid = _project(client, "JIRADASH", "jira")
    r = client.post("/entries", data={
        "entry_date": "2026-05-28", "project_id": str(pid), "duration_minutes": "30",
        "description": "dash jira", "meta__jira__issue_key": "ABC-7",
    }, follow_redirects=False)
    assert r.status_code == 302
    h = {"Authorization": f"Bearer {_token(client)}"}
    entries = client.get("/api/v1/time-entries", headers=h).json()
    mine = [e for e in entries if e["description"] == "dash jira"][0]
    assert mine["sync_metadata_override"] == {"jira": {"issue_key": "ABC-7"}}


def test_calendar_create_with_meta_is_ready(client):
    _login_session(client)
    pid = _project(client, "JIRACAL", "jira")
    r = client.post("/calendar/entries", json={
        "project_id": pid, "entry_date": "2026-05-27",
        "start_time": "09:00", "end_time": "10:00",
        "meta": {"issue_key": "ABC-42"},
    })
    assert r.status_code == 201
    body = r.json()
    assert body["ready"] is True and body["needs_sync"] is True


def test_dashboard_flags_missing_ticket(client):
    _login_session(client)
    pid = _project(client, "JIRAFLAG", "jira")
    _entry(client, pid, description="needs ticket")  # no issue_key → not ready
    page = client.get("/")
    assert "Jira-Ticket" in page.text


def test_project_list_flags_incomplete(client):
    _login_session(client)
    _project(client, "SFINCOMPLETE", "salesforce")  # no project_id
    page = client.get("/projects")
    assert "unvollständig" in page.text
