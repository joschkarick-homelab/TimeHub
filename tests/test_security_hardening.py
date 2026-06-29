"""Regression tests for the critical security fixes:

* CSRF protection on the cookie-authenticated web UI (C1)
* fail-fast on an insecure SECRET_KEY in production (C2)
"""

import pytest
from pydantic import ValidationError


def _login(client) -> None:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")


# ── C1: CSRF ─────────────────────────────────────────────────────────────────

def test_web_post_without_csrf_token_is_rejected(client):
    """A state-changing web POST without the token must be blocked, even for a
    logged-in session."""
    _login(client)
    client.headers.pop("X-CSRF-Token", None)  # drop the auto-injected token
    r = client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code == 403


def test_web_post_with_valid_csrf_token_succeeds(client):
    """With the token present the same request goes through (sanity check that
    we didn't just break every POST)."""
    _login(client)
    r = client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code in (200, 302)


def test_csrf_token_field_in_form_body_is_accepted(client):
    """The token may also arrive as a form field (classic <form> submit), not
    only as a header."""
    _login(client)
    token = client.headers.pop("X-CSRF-Token", None)
    assert token
    r = client.post(
        "/settings/theme",
        data={"theme": "dark", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)


def test_meta_csrf_token_is_rendered(client):
    _login(client)
    html = client.get("/").text
    assert 'name="csrf-token"' in html


# ── C2: secret-key guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_secret", ["dev-insecure-change-me", "", "   "])
def test_production_rejects_insecure_secret_key(bad_secret):
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(app_env="production", secret_key=bad_secret)


def test_dev_allows_default_secret_key():
    from app.config import Settings

    s = Settings(app_env="dev", secret_key="dev-insecure-change-me")
    assert s.app_env == "dev"


def test_production_accepts_strong_secret_key():
    from app.config import Settings

    s = Settings(app_env="production", secret_key="a-sufficiently-long-random-value")
    assert s.app_env == "production"


# ── H1: secret encryption at rest ────────────────────────────────────────────

def test_encrypt_secret_roundtrip():
    from app.security import _ENC_PREFIX, decrypt_secret, encrypt_secret

    enc = encrypt_secret("hunter2")
    assert enc.startswith(_ENC_PREFIX)
    assert "hunter2" not in enc
    assert decrypt_secret(enc) == "hunter2"


def test_decrypt_legacy_plaintext_passes_through():
    from app.security import decrypt_secret

    # Rows written before encryption have no marker — keep working.
    assert decrypt_secret("legacy-plaintext") == "legacy-plaintext"
    assert decrypt_secret("") == ""


def test_salesforce_credentials_stored_encrypted():
    from app.db import SessionLocal
    from app.services import app_settings as app_settings_svc
    from app.services import salesforce as sf

    db = SessionLocal()
    try:
        sf.save_credentials(db, username="api@org.com", password="s3cr3t-pw",
                            security_token="tok123")
        # Raw persisted value must be ciphertext, not the plaintext password.
        raw_pw = app_settings_svc.get_setting(db, sf.SF_PASSWORD_KEY)
        raw_tok = app_settings_svc.get_setting(db, sf.SF_TOKEN_KEY)
        assert "s3cr3t-pw" not in raw_pw and raw_pw.startswith("enc:1:")
        assert "tok123" not in raw_tok and raw_tok.startswith("enc:1:")
        # But the service layer returns the decrypted values.
        creds = sf.get_credentials(db)
        assert creds["password"] == "s3cr3t-pw"
        assert creds["security_token"] == "tok123"
    finally:
        db.close()


# ── H7: bcrypt password truncation guard ─────────────────────────────────────

def test_hash_password_rejects_overlong_input():
    from app.security import MAX_PASSWORD_BYTES, hash_password

    with pytest.raises(ValueError):
        hash_password("a" * (MAX_PASSWORD_BYTES + 1))


def test_hash_password_accepts_max_length():
    from app.security import MAX_PASSWORD_BYTES, hash_password, verify_password

    pw = "a" * MAX_PASSWORD_BYTES
    assert verify_password(pw, hash_password(pw))
