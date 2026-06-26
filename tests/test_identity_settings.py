"""Hub-identity settings: auth_mode resolution, admin allowlist, base_path.

These are pure additions consumed by later migration tasks. The fail-safe that
matters: resolved_auth_mode must default to "hub" (closed) in production so a
missing AUTH_MODE never accidentally opens dev-bypass in prod.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Keep ambient env from polluting these deterministic checks. Each test sets
    # exactly what it needs; clear everything identity-related up front.
    for k in ("APP_ENV", "AUTH_MODE", "ADMIN_EMAILS", "BASE_PATH", "SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_auth_mode_defaults_closed_in_production(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert Settings().resolved_auth_mode == "hub"


def test_auth_mode_defaults_open_outside_production(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    assert Settings().resolved_auth_mode == "dev-bypass"


def test_auth_mode_explicit_value_wins(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("AUTH_MODE", "dev-bypass")
    assert Settings().resolved_auth_mode == "dev-bypass"


def test_admin_emails_parse_and_lowercase(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_EMAILS", "Rick@mindsquare.de, boss@x.de")
    assert Settings().admin_email_set == {"rick@mindsquare.de", "boss@x.de"}


def test_admin_emails_empty_is_empty_set(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert Settings().admin_email_set == set()


def test_normalized_base_path(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "dev")
    for raw, expected in [("/timehub", "/timehub"), ("timehub", "/timehub"),
                          ("/timehub/", "/timehub"), ("", ""), ("/", "")]:
        monkeypatch.setenv("BASE_PATH", raw)
        assert Settings().normalized_base_path == expected
