"""Salesforce sync preview: SOAP-login parsing, the credentials store, the
admin form, and the read-only preview route. The real Salesforce call is never
made — we stub the client."""

from types import SimpleNamespace


def _login_session(client) -> None:
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


def _token(client) -> str:
    return client.post("/api/v1/auth/login",
                       json={"email": "admin@example.com", "password": "testpass"}).json()["access_token"]


# ---------- pure SOAP parsing ----------

_LOGIN_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns="urn:partner.soap.sforce.com">
  <soapenv:Body>
    <loginResponse>
      <result>
        <serverUrl>https://eu40.salesforce.com/services/Soap/u/60.0/00DABCDEFGHIJ</serverUrl>
        <sessionId>00DABC!SESSION_TOKEN_TEST</sessionId>
      </result>
    </loginResponse>
  </soapenv:Body>
</soapenv:Envelope>"""

_LOGIN_FAULT = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <soapenv:Fault>
      <faultcode>sf:INVALID_LOGIN</faultcode>
      <faultstring>INVALID_LOGIN: Invalid username, password, security token; or user locked out.</faultstring>
    </soapenv:Fault>
  </soapenv:Body>
</soapenv:Envelope>"""


def test_parse_login_response_success():
    from app.services.salesforce import _parse_login_response
    session_id, instance_url = _parse_login_response(_LOGIN_OK)
    assert session_id == "00DABC!SESSION_TOKEN_TEST"
    assert instance_url == "https://eu40.salesforce.com"


def test_parse_login_response_fault():
    import pytest
    from app.services.salesforce import _parse_login_response, SalesforceError
    with pytest.raises(SalesforceError, match="INVALID_LOGIN"):
        _parse_login_response(_LOGIN_FAULT)


def test_describe_sobject_hits_right_endpoint_and_parses(monkeypatch):
    """describe_sobject uses /sobjects/<Name>/describe and returns the parsed JSON."""
    from app.services import salesforce as sfs
    captured = {}

    def fake_http(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        return 200, b'{"name":"Projektbesetzung__c","label":"Projektbesetzung","custom":true,"fields":[{"name":"Id","label":"ID","type":"id","nillable":false}]}'

    client = sfs.SalesforceClient("u", "p", "")
    client.session_id = "FAKE"
    client.instance_url = "https://x.salesforce.com"
    monkeypatch.setattr(sfs, "_http", fake_http)
    meta = sfs.describe_sobject(client, "Projektbesetzung__c")
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/sobjects/Projektbesetzung__c/describe")
    assert meta["name"] == "Projektbesetzung__c"
    assert meta["fields"][0]["name"] == "Id"


def test_coerce_bool_lenient():
    from app.services.salesforce import _coerce_bool
    for truthy in ("true", "True", "1", "yes", "ja", "x", "wahr", "Y", True):
        assert _coerce_bool(truthy) is True, truthy
    for falsy in ("false", "0", "no", "nein", "", None, False, "random"):
        assert _coerce_bool(falsy) is False, falsy


def test_snap_quarter_rounds_to_nearest_15():
    from app.services.salesforce import _snap_quarter
    assert _snap_quarter(9, 0) == (9, "00")
    assert _snap_quarter(9, 7) == (9, "00")     # round down
    assert _snap_quarter(9, 8) == (9, "15")     # round up
    assert _snap_quarter(9, 30) == (9, "30")
    assert _snap_quarter(9, 53) == (10, "00")   # rolls over
    assert _snap_quarter(0, 90) == (1, "30")    # 90 min into the day


def test_build_zeiterfassung_payload_without_start_end():
    from datetime import date as _date
    from types import SimpleNamespace
    from app.services.salesforce import build_zeiterfassung_payload
    entry = SimpleNamespace(
        entry_date=_date(2026, 5, 27), start_time=None, end_time=None,
        duration_minutes=90, description="Demo-Beschreibung",
    )
    payload = build_zeiterfassung_payload(entry, "a0Q000MAY26", remote_value="true")
    assert payload["Kontierungsmonat__c"] == "a0Q000MAY26"
    assert payload["Tag__c"] == "2026-05-27"
    assert payload["Arbeitszeit__c"] == 1.5
    assert payload["Arbeitszeit_Minuten__c"] == 90
    assert payload["Von_Stunde__c"] == 0 and payload["Von_Minute__c"] == "00"
    assert payload["Bis_Stunde__c"] == 1 and payload["Bis_Minute__c"] == "30"
    assert payload["Pause__c"] == 0
    assert payload["Taetigkeitsbeschreibung__c"] == "Demo-Beschreibung"
    assert payload["Remote__c"] is True


def test_build_zeiterfassung_payload_with_start_end_clips_long_description():
    from datetime import date as _date, time as _time
    from types import SimpleNamespace
    from app.services.salesforce import build_zeiterfassung_payload
    entry = SimpleNamespace(
        entry_date=_date(2026, 5, 27),
        start_time=_time(9, 0), end_time=_time(10, 30),
        duration_minutes=90, description="x" * 300,
    )
    payload = build_zeiterfassung_payload(entry, "a0Q000MAY26")
    assert payload["Von_Stunde__c"] == 9 and payload["Von_Minute__c"] == "00"
    assert payload["Bis_Stunde__c"] == 10 and payload["Bis_Minute__c"] == "30"
    assert len(payload["Taetigkeitsbeschreibung__c"]) == 255
    assert payload["Remote__c"] is False  # no remote value passed


def test_preview_skips_when_kontierungsmonat_not_open(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFPREVSTATUS")
    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period",
                        lambda _c, _aid, _date: dict(_FAKE_PERIOD, status="in Bearbeitung"))
    r = client.post("/sync/salesforce/preview", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert "nicht offen" in r.text
    assert "in Bearbeitung" in r.text


def test_preview_explicit_vor_ort_overrides_default(client, monkeypatch):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    pid = client.post("/api/v1/projects", json={
        "name": "SF Vor Ort", "code": "SFVORORT",
        "default_sync_target": "salesforce",
        "sync_metadata": {"salesforce": {"assignment_id": "a01000000000001"}},
    }, headers=h).json()["id"]
    eid = client.post("/api/v1/time-entries", json={
        "project_id": pid, "entry_date": "2026-05-27", "duration_minutes": 60,
        "description": "vor ort",
        "sync_metadata_override": {"salesforce": {"remote": "false"}},
    }, headers=h).json()["id"]
    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period", lambda _c, _aid, _date: dict(_FAKE_PERIOD))
    r = client.post("/sync/salesforce/preview", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert '"Remote__c": false' in r.text
    assert "Vor Ort" in r.text


def test_list_assignments_for_user_query_shape(monkeypatch):
    """list_assignments_for_user matcht intern UND extern auf die E-Mail."""
    from app.services import salesforce as sfs
    captured = {}
    def fake_query(self, soql):
        captured["soql"] = soql
        return {"records": [
            {"Id": "a01000000000111", "Name": "PB-111", "Projektbezeichnung__c": "Foo"},
            {"Id": "a01000000000222", "Name": "PB-222", "Projektbezeichnung__c": None},
        ]}
    monkeypatch.setattr(sfs.SalesforceClient, "query", fake_query)
    client = sfs.SalesforceClient("u", "p", "")
    client.session_id = "x"; client.instance_url = "https://x"
    items = sfs.list_assignments_for_user(client, "rick@mindsquare.de")
    assert "rick@mindsquare.de" in captured["soql"]
    assert "Mitarbeiter__r.Email" in captured["soql"]
    assert "Externe_Projektbesetzung__r.Email" in captured["soql"]
    assert "Geschlossen__c = false" in captured["soql"]
    assert items == [
        {"value": "a01000000000111", "label": "Foo (PB-111)"},
        {"value": "a01000000000222", "label": "PB-222"},
    ]


def test_list_assignments_for_user_rejects_bad_email():
    from app.services import salesforce as sfs
    client = sfs.SalesforceClient("u", "p", "")
    client.session_id = "x"; client.instance_url = "https://x"
    # No SOQL is sent for obviously broken/injecting emails
    assert sfs.list_assignments_for_user(client, "evil'--") == []
    assert sfs.list_assignments_for_user(client, "") == []


def test_describe_sobject_rejects_garbage_names():
    import pytest
    from app.services.salesforce import SalesforceClient, SalesforceError, describe_sobject
    client = SalesforceClient("u", "p", "")
    client.session_id = "x"; client.instance_url = "https://x"
    with pytest.raises(SalesforceError):
        describe_sobject(client, "Bad Name; DROP")


def test_ensure_id_rejects_bad_input():
    import pytest
    from app.services.salesforce import _ensure_id, SalesforceError
    assert _ensure_id("a01000000000001") == "a01000000000001"
    with pytest.raises(SalesforceError):
        _ensure_id("short")
    with pytest.raises(SalesforceError):
        _ensure_id("a01000000' OR 1=1--")


# ---------- credentials store ----------

def test_credentials_round_trip(client):
    from app.db import SessionLocal
    from app.services import salesforce as sfs
    with SessionLocal() as db:
        sfs.save_credentials(db, username="api.user@x", password="pw1",
                              security_token="tok1", login_url="https://test.salesforce.com",
                              api_version="60.0")
        c = sfs.get_credentials(db)
        assert c["username"] == "api.user@x"
        assert c["password"] == "pw1"
        assert c["security_token"] == "tok1"
        # empty password keeps the existing one
        sfs.save_credentials(db, password="", security_token="")
        c2 = sfs.get_credentials(db)
        assert c2["password"] == "pw1" and c2["security_token"] == "tok1"
        # explicit clear drops the stored token (for users that don't need one)
        sfs.save_credentials(db, clear_security_token=True)
        assert sfs.get_credentials(db)["security_token"] == ""


def test_settings_route_can_clear_token(client):
    _login_session(client)
    # Seed a token first
    client.post("/settings/salesforce", data={
        "sf_username": "u", "sf_password": "p", "sf_security_token": "STALE",
        "sf_login_url": "https://x", "sf_api_version": "60.0",
    }, follow_redirects=False)
    # Now drop it via the checkbox without touching the password
    client.post("/settings/salesforce", data={
        "sf_username": "u", "sf_password": "", "sf_security_token": "",
        "sf_clear_token": "true",
    }, follow_redirects=False)
    from app.db import SessionLocal
    from app.services import salesforce as sfs
    with SessionLocal() as db:
        c = sfs.get_credentials(db)
        assert c["password"] == "p"  # preserved
        assert c["security_token"] == ""  # cleared


def test_settings_salesforce_route_persists(client):
    _login_session(client)
    r = client.post("/settings/salesforce", data={
        "sf_username": "api.user@firma.com",
        "sf_password": "secret",
        "sf_security_token": "TOKEN",
        "sf_login_url": "https://test.salesforce.com",
        "sf_api_version": "60.0",
    }, follow_redirects=False)
    assert r.status_code == 302
    page = client.get("/users")
    # username visible; password/token marked "gesetzt" but never echoed
    assert "api.user@firma.com" in page.text
    assert "gesetzt" in page.text
    assert "secret" not in page.text and "TOKEN" not in page.text


# ---------- preview route (stubbed client) ----------

def _make_project_and_entry(client, code: str, entry_date: str = "2026-05-27"):
    """Admin: project with target=salesforce + assignment_id; one entry."""
    h = {"Authorization": f"Bearer {_token(client)}"}
    r = client.post("/api/v1/projects", json={
        "name": f"SF Demo {code}", "code": code,
        "default_sync_target": "salesforce",
        "sync_metadata": {"salesforce": {"assignment_id": "a01000000000001"}},
    }, headers=h)
    if r.status_code == 201:
        pid = r.json()["id"]
    else:
        pid = next(p["id"] for p in client.get("/api/v1/projects", headers=h).json()
                   if p["code"] == code)
    e = client.post("/api/v1/time-entries", json={
        "project_id": pid, "entry_date": entry_date, "duration_minutes": 90,
        "description": f"demo {code}",
    }, headers=h).json()
    return pid, e["id"]


def _fake_client():
    """Stand-in SalesforceClient for the preview tests."""
    c = SimpleNamespace()
    c.session_id = "FAKE"
    c.instance_url = "https://test.salesforce.com"
    return c


_FAKE_ASSIGNMENT = {
    "id": "a01000000000001", "name": "PB-001",
    "project_id": "a0P000000000001", "project_name": "Demo Project",
    "resource_id": "0050000000RES", "resource_name": "Max Mustermann",
    "is_external": False, "closed": False, "active": "Ja",
}
_FAKE_PERIOD = {
    "id": "a0Q000MAY26", "name": "Kontierungsmonat 05/2026",
    "start_date": "2026-05-01", "end_date": "2026-05-31",
    "status": "offen", "closed": False,
}


def test_preview_renders_zeiterfassung_payload(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFPREV1")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period", lambda _c, _aid, _date: dict(_FAKE_PERIOD))

    r = client.post("/sync/salesforce/preview",
                    data={"entry_ids": str(eid)},
                    follow_redirects=False)
    assert r.status_code == 200, r.text
    body = r.text
    # Header: project, employee, accounting month
    assert "Demo Project" in body
    assert "Max Mustermann" in body
    assert "Kontierungsmonat 05/2026" in body
    # Payload fields of Zeiterfassung__c
    assert "Kontierungsmonat__c" in body
    assert "Tag__c" in body
    assert "Arbeitszeit__c" in body
    assert "Arbeitszeit_Minuten__c" in body
    assert "Von_Stunde__c" in body
    assert "Bis_Stunde__c" in body
    assert "Taetigkeitsbeschreibung__c" in body
    assert "Pause__c" in body
    assert "Remote__c" in body
    # Default für Remote ist "Remote" → ohne expliziten Wert: Remote-Badge + true
    assert ">Remote</span>" in body
    assert '"Remote__c": true' in body
    # 90 Minuten ohne Start/Ende → Von_Stunde 0, Bis_Stunde 1, Bis_Minute 30
    assert '"Von_Stunde__c": 0' in body
    assert '"Bis_Stunde__c": 1' in body
    assert '"Bis_Minute__c": "30"' in body
    # Confirm-Form für /sync/salesforce/execute (Push aktiv)
    assert 'action="/sync/salesforce/execute"' in body
    assert "Synchronisieren bestätigen" in body


def test_preview_skips_when_assignment_missing_in_sf(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFPREVMISS")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: None)  # not found
    monkeypatch.setattr(sfs, "get_monthly_period",
                        lambda _c, _aid, _date: dict(_FAKE_PERIOD))

    r = client.post("/sync/salesforce/preview", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert "nicht in SF gefunden" in r.text


def test_preview_remote_flag_from_sync_metadata(client, monkeypatch):
    """A 'remote' value in sync_metadata_override.salesforce → Remote__c=True."""
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    pid = client.post("/api/v1/projects", json={
        "name": "SF Remote", "code": "SFRMT",
        "default_sync_target": "salesforce",
        "sync_metadata": {"salesforce": {"assignment_id": "a01000000000001"}},
    }, headers=h).json()["id"]
    eid = client.post("/api/v1/time-entries", json={
        "project_id": pid, "entry_date": "2026-05-27", "duration_minutes": 60,
        "description": "remote demo",
        "sync_metadata_override": {"salesforce": {"remote": "true"}},
    }, headers=h).json()["id"]

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period", lambda _c, _aid, _date: dict(_FAKE_PERIOD))

    r = client.post("/sync/salesforce/preview", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    body = r.text
    # Remote-Badge sichtbar UND Remote__c: true im Payload
    assert "<span class=\"text-emerald-700" in body and "Remote</span>" in body
    assert '"Remote__c": true' in body


def test_preview_without_credentials_shows_hint(client):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFPREVNOCRED")
    # Clear any leftover SF creds from earlier tests.
    from app.db import SessionLocal
    from app.services import app_settings as aps
    with SessionLocal() as db:
        for k in ("sf.username", "sf.password"):
            aps.set_setting(db, k, "")
    r = client.post("/sync/salesforce/preview", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert "Salesforce-Zugangsdaten" in r.text


def test_dashboard_shows_sync_button_when_configured(client):
    _login_session(client)
    # configure SF + create a sync-ready salesforce entry
    from app.db import SessionLocal
    from app.services import salesforce as sfs
    with SessionLocal() as db:
        sfs.save_credentials(db, username="u", password="p", security_token="t")
    # current month per the test env clock (see conftest / env)
    _make_project_and_entry(client, "SFDASH", entry_date="2026-06-01")
    page = client.get("/")
    assert "Auswahl in Salesforce-Vorschau" in page.text


# ---------- create_zeiterfassung (REST POST) ----------

def test_create_zeiterfassung_returns_id_on_success(monkeypatch):
    """201 success → URL + headers stimmen, neue Id wird zurückgegeben."""
    from app.services import salesforce as sfs
    captured = {}

    def fake_http(method, url, *, data=None, headers=None, timeout=30):
        captured["method"] = method
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return 201, b'{"id":"a0Z000000000ABC","success":true,"errors":[]}'

    monkeypatch.setattr(sfs, "_http", fake_http)
    c = sfs.SalesforceClient("u", "p", "")
    c.session_id = "SESSION"
    c.instance_url = "https://test.salesforce.com"
    new_id = sfs.create_zeiterfassung(c, {"Tag__c": "2026-05-27", "Arbeitszeit__c": 1.5})
    assert new_id == "a0Z000000000ABC"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/sobjects/Zeiterfassung__c")
    assert captured["headers"]["Authorization"] == "Bearer SESSION"
    assert captured["headers"]["Content-Type"] == "application/json"
    import json as _json
    sent = _json.loads(captured["data"])
    assert sent["Tag__c"] == "2026-05-27"
    assert sent["Arbeitszeit__c"] == 1.5


def test_create_zeiterfassung_raises_with_formatted_error(monkeypatch):
    """400 mit SF-Errors-Array → SalesforceError mit Code + Message + Felder."""
    from app.services import salesforce as sfs
    err_body = (b'[{"message":"Required field missing: Tag__c",'
                b'"errorCode":"REQUIRED_FIELD_MISSING","fields":["Tag__c"]}]')
    monkeypatch.setattr(sfs, "_http", lambda *a, **kw: (400, err_body))
    c = sfs.SalesforceClient("u", "p", "")
    c.session_id = "SESSION"
    c.instance_url = "https://test.salesforce.com"
    try:
        sfs.create_zeiterfassung(c, {"foo": "bar"})
    except sfs.SalesforceError as e:
        msg = str(e)
        assert "REQUIRED_FIELD_MISSING" in msg
        assert "Required field missing" in msg
        assert "Tag__c" in msg
    else:
        raise AssertionError("Expected SalesforceError")


# ---------- /sync/salesforce/execute (stubbed client + create) ----------

def test_execute_creates_and_marks_synced(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFEXEC1")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period", lambda _c, _aid, _date: dict(_FAKE_PERIOD))
    monkeypatch.setattr(sfs, "create_zeiterfassung", lambda _c, _payload: "a0Z000000000XYZ")

    r = client.post("/sync/salesforce/execute",
                    data={"entry_ids": str(eid)}, follow_redirects=False)
    assert r.status_code == 200, r.text
    body = r.text
    assert "Ergebnis" in body
    assert "a0Z000000000XYZ" in body

    # Persistenz prüfen: sync_status=synced, Id im sync_metadata_override
    from app.db import SessionLocal
    from app.models import TimeEntry
    with SessionLocal() as db:
        e = db.get(TimeEntry, eid)
        assert e.sync_status == "synced"
        meta = (e.sync_metadata_override or {}).get("salesforce") or {}
        assert meta.get("zeiterfassung_id") == "a0Z000000000XYZ"


def test_execute_marks_failed_on_sf_error_per_entry(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFEXECFAIL")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period", lambda _c, _aid, _date: dict(_FAKE_PERIOD))

    def boom(_c, _payload):
        raise sfs.SalesforceError("[FIELD_INTEGRITY_EXCEPTION] kaputt")

    monkeypatch.setattr(sfs, "create_zeiterfassung", boom)

    r = client.post("/sync/salesforce/execute", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert "FIELD_INTEGRITY_EXCEPTION" in r.text

    from app.db import SessionLocal
    from app.models import TimeEntry
    with SessionLocal() as db:
        e = db.get(TimeEntry, eid)
        assert e.sync_status == "failed"
        meta = (e.sync_metadata_override or {}).get("salesforce") or {}
        assert "zeiterfassung_id" not in meta


def test_execute_skips_already_synced_entries(client, monkeypatch):
    """Bereits synchronisierte Einträge werden idempotent übersprungen,
    create_zeiterfassung darf gar nicht aufgerufen werden."""
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFEXECSKIP")

    from app.db import SessionLocal
    from app.models import TimeEntry
    with SessionLocal() as db:
        e = db.get(TimeEntry, eid)
        e.sync_status = "synced"
        e.sync_metadata_override = {"salesforce": {"zeiterfassung_id": "a0Z000000000OLD"}}
        db.add(e); db.commit()

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period", lambda _c, _aid, _date: dict(_FAKE_PERIOD))

    def must_not_call(_c, _p):
        raise AssertionError("create_zeiterfassung must not be called for synced entries")

    monkeypatch.setattr(sfs, "create_zeiterfassung", must_not_call)

    r = client.post("/sync/salesforce/execute", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    body = r.text
    assert "bereits synchronisiert" in body
    assert "a0Z000000000OLD" in body


def test_execute_revalidates_period_status_before_write(client, monkeypatch):
    """Auch wenn die Vorschau bestanden wurde: ändert sich der Kontierungsmonat-Status
    auf z.B. 'in Bearbeitung', wird der Eintrag mit Fehler markiert (kein POST)."""
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFEXECREVAL")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: dict(_FAKE_ASSIGNMENT, id=aid))
    monkeypatch.setattr(sfs, "get_monthly_period",
                        lambda _c, _aid, _date: dict(_FAKE_PERIOD, status="in Bearbeitung"))

    def must_not_call(_c, _p):
        raise AssertionError("create_zeiterfassung must not be called when period is not open")

    monkeypatch.setattr(sfs, "create_zeiterfassung", must_not_call)

    r = client.post("/sync/salesforce/execute", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert "nicht offen" in r.text

    from app.db import SessionLocal
    from app.models import TimeEntry
    with SessionLocal() as db:
        e = db.get(TimeEntry, eid)
        assert e.sync_status == "failed"
