"""Per-user Salesforce OAuth: Connected-App config store, authorize URL, token
persistence, the OAuth-backed client (bearer auth + 401 refresh), per-user
client selection with service-account fallback, and the connect/callback routes.

The HTTP seam (``salesforce._http``) is stubbed so nothing leaves the process.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.security import decrypt_secret, encrypt_secret
from app.services import salesforce as sf_svc


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _admin(db):
    from app.models import User

    return db.execute(select(User).where(User.email == "admin@example.com")).scalar_one()


@pytest.fixture
def oauth_configured(db):
    sf_svc.save_oauth_config(
        db,
        client_id="3MVG9consumerkey",
        client_secret="consumer-secret",
        login_url="https://login.salesforce.com",
    )
    return db


@pytest.fixture
def clean_sf_connection(db):
    """Remove the admin's SF connection after the test so other suites that build
    a client for the admin don't pick up a stale OAuth row."""
    yield
    from app.models import SalesforceConnection

    admin = _admin(db)
    conn = db.execute(
        select(SalesforceConnection).where(SalesforceConnection.user_id == admin.id)
    ).scalar_one_or_none()
    if conn is not None:
        db.delete(conn)
        db.commit()


# ── config store ──────────────────────────────────────────────────────────────

def test_oauth_config_roundtrip_encrypts_secret(db):
    sf_svc.save_oauth_config(db, client_id="cid", client_secret="sshh", login_url="https://x")
    cfg = sf_svc.get_oauth_config(db)
    assert cfg["client_id"] == "cid"
    assert cfg["client_secret"] == "sshh"  # decrypted back
    assert sf_svc.oauth_configured(db) is True

    from app.services import app_settings as app_settings_svc

    stored = app_settings_svc.get_setting(db, sf_svc.SF_OAUTH_CLIENT_SECRET_KEY, "")
    assert stored.startswith("enc:1:")
    assert "sshh" not in stored


def test_oauth_empty_secret_keeps_existing(db):
    sf_svc.save_oauth_config(db, client_id="cid", client_secret="keep-me")
    sf_svc.save_oauth_config(db, client_id="cid2", client_secret="")  # blank → unchanged
    assert sf_svc.get_oauth_config(db)["client_secret"] == "keep-me"


# ── authorize URL ──────────────────────────────────────────────────────────────

def test_oauth_authorize_url_carries_params(oauth_configured):
    url = sf_svc.oauth_authorize_url(
        oauth_configured, state="st8", code_challenge="chal",
        redirect_uri="https://h/salesforce/oauth/callback",
    )
    parsed = urlparse(url)
    assert parsed.netloc == "login.salesforce.com"
    assert parsed.path == "/services/oauth2/authorize"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["3MVG9consumerkey"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["st8"]
    assert q["code_challenge"] == ["chal"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["api refresh_token"]


def test_make_pkce_is_valid_s256():
    import base64
    import hashlib

    verifier, challenge = sf_svc.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected


# ── token persistence ──────────────────────────────────────────────────────────

def test_store_oauth_tokens_encrypts_and_sets_fields():
    from app.models import SalesforceConnection

    conn = SalesforceConnection(user_id=1)
    sf_svc.store_oauth_tokens(
        conn=conn, db=None,
        token_response={
            "access_token": "AT", "refresh_token": "RT",
            "instance_url": "https://eu.my.salesforce.com",
        },
        account="me@firma.com",
    )
    assert decrypt_secret(conn.access_token) == "AT"
    assert decrypt_secret(conn.refresh_token) == "RT"
    assert conn.instance_url == "https://eu.my.salesforce.com"
    assert conn.account == "me@firma.com"
    # No expires_in → expiry stays unset (we rely on the 401 refresh backstop).
    assert conn.token_expires_at is None


def test_store_oauth_tokens_refresh_keeps_existing_refresh_token():
    from app.models import SalesforceConnection

    conn = SalesforceConnection(user_id=1, refresh_token=encrypt_secret("OLD-RT"))
    sf_svc.store_oauth_tokens(
        conn=conn, db=None, token_response={"access_token": "NEW-AT"}  # no refresh_token
    )
    assert decrypt_secret(conn.access_token) == "NEW-AT"
    assert decrypt_secret(conn.refresh_token) == "OLD-RT"


# ── OAuth-backed client ─────────────────────────────────────────────────────────

def test_from_oauth_client_uses_bearer_without_soap_login(monkeypatch):
    calls = []

    def fake_http(method, url, *, data=None, headers=None, timeout=30):
        calls.append((method, url, headers))
        return 200, b'{"records": []}'

    monkeypatch.setattr(sf_svc, "_http", fake_http)
    client = sf_svc.SalesforceClient.from_oauth(
        access_token="TOK", instance_url="https://eu.my.salesforce.com"
    )
    client.query("SELECT Id FROM Account")
    # Single GET, bearer = the OAuth token, no SOAP login POST.
    assert len(calls) == 1
    method, url, headers = calls[0]
    assert method == "GET"
    assert headers["Authorization"] == "Bearer TOK"
    assert "/services/data/" in url


def test_oauth_client_refreshes_on_401(monkeypatch):
    responses = [(401, b"expired"), (200, b'{"records": []}')]

    def fake_http(method, url, *, data=None, headers=None, timeout=30):
        status, body = responses.pop(0)
        fake_http.last_auth = headers["Authorization"]
        return status, body

    monkeypatch.setattr(sf_svc, "_http", fake_http)
    client = sf_svc.SalesforceClient.from_oauth(
        access_token="STALE", instance_url="https://eu.my.salesforce.com"
    )
    refreshed = {"n": 0}

    def reauth():
        refreshed["n"] += 1
        return "FRESH"

    client._reauth = reauth
    client.query("SELECT Id FROM Account")
    assert refreshed["n"] == 1
    assert fake_http.last_auth == "Bearer FRESH"  # retried with the new token


# ── per-user client selection ───────────────────────────────────────────────────

def test_client_for_user_prefers_connection(db, clean_sf_connection):
    from app.models import SalesforceConnection

    admin = _admin(db)
    conn = SalesforceConnection(
        user_id=admin.id, account="me@firma.com",
        instance_url="https://eu.my.salesforce.com",
        access_token=encrypt_secret("CONNTOK"),
        refresh_token=encrypt_secret("CONNRT"),
    )
    db.add(conn)
    db.commit()

    client = sf_svc.client_for_user(db, admin)
    assert client is not None
    assert client.session_id == "CONNTOK"
    assert client.instance_url == "https://eu.my.salesforce.com"
    assert client._reauth is not None  # refresh hook wired


def test_client_for_user_falls_back_to_service_account(db, monkeypatch):
    admin = _admin(db)
    # No connection for admin; pretend the global SOAP creds are configured.
    monkeypatch.setattr(
        sf_svc, "client_from_settings",
        lambda _db: sf_svc.SalesforceClient("svc@firma.com", "pw"),
    )
    client = sf_svc.client_for_user(db, admin)
    assert client is not None
    assert client.username == "svc@firma.com"


def test_available_for_user_true_via_connection_without_global(db, clean_sf_connection, monkeypatch):
    from app.models import SalesforceConnection

    admin = _admin(db)
    monkeypatch.setattr(sf_svc, "credentials_configured", lambda _db: False)
    assert sf_svc.available_for_user(db, admin) is False
    db.add(SalesforceConnection(user_id=admin.id, access_token=encrypt_secret("X")))
    db.commit()
    assert sf_svc.available_for_user(db, admin) is True


# ── routes ──────────────────────────────────────────────────────────────────────

def test_connect_requires_configuration(client, db):
    from app.models import AppSetting

    for key in (sf_svc.SF_OAUTH_CLIENT_ID_KEY, sf_svc.SF_OAUTH_CLIENT_SECRET_KEY):
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()

    _login_session(client)
    r = client.get("/salesforce/oauth/connect", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/profile?error=")


def test_connect_redirects_to_salesforce(client, oauth_configured):
    _login_session(client)
    r = client.get("/salesforce/oauth/connect", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(
        "https://login.salesforce.com/services/oauth2/authorize"
    )


def test_callback_rejects_bad_state(client, oauth_configured):
    _login_session(client)
    r = client.get(
        "/salesforce/oauth/callback?code=abc&state=forged", follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/profile?error=")


def test_callback_stores_connection(client, oauth_configured, monkeypatch, db, clean_sf_connection):
    _login_session(client)
    start = client.get("/salesforce/oauth/connect", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    monkeypatch.setattr(
        sf_svc, "oauth_exchange_code",
        lambda *a, **k: {
            "access_token": "AT", "refresh_token": "RT",
            "instance_url": "https://eu.my.salesforce.com",
        },
    )
    monkeypatch.setattr(sf_svc, "oauth_userinfo", lambda *a, **k: "me@firma.com")

    r = client.get(
        f"/salesforce/oauth/callback?code=good&state={state}", follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/profile?flash=")

    from app.models import SalesforceConnection

    admin = _admin(db)
    conn = db.execute(
        select(SalesforceConnection).where(SalesforceConnection.user_id == admin.id)
    ).scalar_one()
    assert conn.account == "me@firma.com"
    assert decrypt_secret(conn.access_token) == "AT"
    assert conn.instance_url == "https://eu.my.salesforce.com"
