"""Microsoft 365 single sign-on (OIDC): ID-token validation, the login
authorize URL, callback state/nonce hardening, user matching + auto-provisioning
and the password-login guard for SSO-only accounts.

The ID-token signature path is exercised for real: tests mint RS256 tokens with
a locally generated key and stub only the network seams (OIDC discovery + the
JWKS key lookup), so audience/issuer/expiry/nonce checks run end-to-end offline.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from app.services import m365 as m365_svc

TENANT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "client-app-id"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def configured(db):
    """A concrete-tenant config so the issuer check pins to one organisation."""
    m365_svc.save_config(db, client_id=CLIENT_ID, client_secret="sec", tenant=TENANT_ID)
    return db


@pytest.fixture
def oidc_stub(monkeypatch, signing_key, configured):
    """Stub the two network seams: discovery (issuer + jwks_uri) and the JWKS
    key resolver (returns our local public key)."""
    monkeypatch.setattr(
        m365_svc,
        "_fetch_discovery",
        lambda _db: {"issuer": ISSUER, "jwks_uri": "https://stub/keys"},
    )
    monkeypatch.setattr(
        m365_svc, "_resolve_signing_key", lambda _uri, _tok: signing_key.public_key()
    )


def _make_token(signing_key, **overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "tid": TENANT_ID,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "nonce": "the-nonce",
        "oid": "oid-abc",
        "email": "person@contoso.com",
        "name": "Test Person",
    }
    claims.update(overrides)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test"})


# ── ID-token validation ───────────────────────────────────────────────────────

def test_validate_id_token_accepts_valid_token(db, signing_key, oidc_stub):
    token = _make_token(signing_key)
    claims = m365_svc.validate_id_token(db, token, nonce="the-nonce")
    assert claims["email"] == "person@contoso.com"
    assert claims["oid"] == "oid-abc"


def test_validate_id_token_rejects_expired(db, signing_key, oidc_stub):
    token = _make_token(signing_key, exp=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(m365_svc.M365Error):
        m365_svc.validate_id_token(db, token, nonce="the-nonce")


def test_validate_id_token_rejects_wrong_audience(db, signing_key, oidc_stub):
    token = _make_token(signing_key, aud="some-other-app")
    with pytest.raises(m365_svc.M365Error):
        m365_svc.validate_id_token(db, token, nonce="the-nonce")


def test_validate_id_token_rejects_wrong_issuer(db, signing_key, oidc_stub):
    other = "https://login.microsoftonline.com/99999999-0000-0000-0000-000000000000/v2.0"
    token = _make_token(signing_key, iss=other, tid="99999999-0000-0000-0000-000000000000")
    with pytest.raises(m365_svc.M365Error):
        m365_svc.validate_id_token(db, token, nonce="the-nonce")


def test_validate_id_token_rejects_bad_nonce(db, signing_key, oidc_stub):
    token = _make_token(signing_key, nonce="attacker-nonce")
    with pytest.raises(m365_svc.M365Error):
        m365_svc.validate_id_token(db, token, nonce="the-nonce")


def test_validate_id_token_rejects_bad_signature(db, signing_key, oidc_stub):
    # Signed by a different key than the resolver returns → signature fails.
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token(other_key)
    with pytest.raises(m365_svc.M365Error):
        m365_svc.validate_id_token(db, token, nonce="the-nonce")


def test_profile_from_claims_normalizes_email():
    p = m365_svc.profile_from_claims({"preferred_username": "Mixed.Case@Contoso.COM", "oid": "x"})
    assert p["email"] == "mixed.case@contoso.com"
    assert p["oid"] == "x"


# ── authorize URL (login variant) ─────────────────────────────────────────────

def test_login_authorize_url_uses_oidc_scope_and_nonce(configured):
    url = m365_svc.authorize_url(
        configured,
        state="st",
        code_challenge="ch",
        redirect_uri="https://h/auth/m365/callback",
        scope=m365_svc.OIDC_SCOPES,
        nonce="n0nce",
    )
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["openid profile email"]
    assert "Calendars.Read" not in q["scope"][0]
    assert q["nonce"] == ["n0nce"]


# ── login routes ──────────────────────────────────────────────────────────────

def test_login_page_shows_button_when_configured(client, configured):
    html = client.get("/login").text
    assert "/auth/m365/login" in html
    assert "Mit Microsoft 365 anmelden" in html


def test_login_start_redirects_to_microsoft(client, configured):
    r = client.get("/auth/m365/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://login.microsoftonline.com/")


def test_callback_rejects_bad_state(client, configured):
    # No m365_login in session → state can't match → bounce to /login error.
    r = client.get("/auth/m365/callback?code=abc&state=forged", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?error=")


def _drive_callback(client, monkeypatch, claims: dict):
    """Start the real login (to seed session state), then complete the callback
    with token exchange + validation stubbed to return `claims`."""
    start = client.get("/auth/m365/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    monkeypatch.setattr(m365_svc, "exchange_code", lambda *a, **k: {"id_token": "tok"})
    monkeypatch.setattr(m365_svc, "validate_id_token", lambda *a, **k: claims)
    return client.get(
        f"/auth/m365/callback?code=good&state={state}", follow_redirects=False
    )


def test_callback_logs_in_existing_user_by_email(client, configured, monkeypatch):
    r = _drive_callback(
        client, monkeypatch, {"email": "admin@example.com", "oid": "", "name": "Admin"}
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # Session is live: a protected page now renders instead of redirecting.
    assert client.get("/profile", follow_redirects=False).status_code == 200


def test_callback_auto_provisions_unknown_email(client, configured, monkeypatch, db):
    email = "newhire@contoso.com"
    r = _drive_callback(
        client, monkeypatch, {"email": email, "oid": "oid-new", "name": "New Hire"}
    )
    assert r.status_code == 302 and r.headers["location"] == "/"
    User = _user_model()
    created = db.execute(
        select(User).where(func.lower(User.email) == email)
    ).scalar_one()
    assert created.is_active is True
    assert created.is_admin is False
    assert created.hashed_password is None
    assert created.entra_oid == "oid-new"


def test_callback_matches_existing_by_oid_and_backfills(client, configured, monkeypatch, db):
    User = _user_model()
    u = User(email="match@contoso.com", full_name="M", hashed_password=None,
             is_active=True, entra_oid="oid-stable")
    db.add(u)
    db.commit()
    # Email differs (rename), but the stable oid still matches the existing row.
    r = _drive_callback(
        client, monkeypatch,
        {"email": "renamed@contoso.com", "oid": "oid-stable", "name": "M"},
    )
    assert r.status_code == 302 and r.headers["location"] == "/"
    # No duplicate user got created for the new email.
    assert db.execute(
        select(func.count()).select_from(User).where(
            func.lower(User.email) == "renamed@contoso.com"
        )
    ).scalar() == 0


# ── password-login guard ───────────────────────────────────────────────────────

def test_password_login_rejected_for_sso_only_user(client, db):
    User = _user_model()
    u = User(email="ssoonly@contoso.com", full_name="SSO", hashed_password=None,
             is_active=True, entra_oid="oid-sso-only")
    db.add(u)
    db.commit()
    r = client.post(
        "/login",
        data={"email": "ssoonly@contoso.com", "password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def _user_model():
    from app.models import User

    return User
