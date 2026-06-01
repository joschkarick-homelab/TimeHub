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


def test_preview_renders_payload(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFPREV1")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: {
        "id": aid, "name": "Demo Assignment",
        "project_id": "a0P000000000001", "project_name": "Demo Project",
        "resource_id": "0030000000RES", "resource_name": "Max Mustermann",
        "closed": False,
    })
    monkeypatch.setattr(sfs, "get_monthly_time_period", lambda _c, _date: {
        "id": "a0T000MAY26", "name": "Mai 2026",
        "start_date": "2026-05-01", "end_date": "2026-05-31",
    })

    r = client.post("/sync/salesforce/preview",
                    data={"entry_ids": str(eid)},
                    follow_redirects=False)
    assert r.status_code == 200, r.text
    body = r.text
    # The preview shows resolved project + resource, the period, and the payload
    assert "Demo Project" in body
    assert "Max Mustermann" in body
    assert "Mai 2026" in body
    # weekday: 2026-05-27 is a Wednesday → Wednesday_Hours
    assert "pse__Wednesday_Hours__c" in body
    assert "a01000000000001" in body  # assignment id in payload
    # no DML happened — preview banner still says it's a preview
    assert "Push noch nicht aktiv" in body


def test_preview_skips_when_assignment_missing_in_sf(client, monkeypatch):
    _login_session(client)
    _pid, eid = _make_project_and_entry(client, "SFPREVMISS")

    import app.services.salesforce as sfs
    monkeypatch.setattr(sfs, "client_from_settings", lambda db: _fake_client())
    monkeypatch.setattr(sfs, "get_assignment", lambda _c, aid: None)  # not found
    monkeypatch.setattr(sfs, "get_monthly_time_period", lambda _c, _date: {
        "id": "p", "name": "M", "start_date": "x", "end_date": "y"})

    r = client.post("/sync/salesforce/preview", data={"entry_ids": str(eid)})
    assert r.status_code == 200
    assert "nicht gefunden" in r.text


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
