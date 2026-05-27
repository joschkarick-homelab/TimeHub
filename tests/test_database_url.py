"""Verify the discrete-Postgres-env-vars path in Settings.

The deploy regression: a POSTGRES_PASSWORD with URL-unsafe characters
(/, @, :, ...) broke string-interpolated DATABASE_URLs. Settings now builds
the URL via sqlalchemy.URL, so any password works.
"""

import importlib

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Clear anything our conftest set that would short-circuit the postgres path.
    for k in (
        "DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(k, raising=False)


def _fresh_settings():
    # Settings is cached via lru_cache; reload to drop the cached instance.
    import app.config

    importlib.reload(app.config)
    return app.config.Settings()


def test_falls_back_to_sqlite_when_nothing_set():
    s = _fresh_settings()
    assert s.database_url.startswith("sqlite:")


def test_explicit_database_url_wins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./explicit.sqlite")
    monkeypatch.setenv("POSTGRES_USER", "ignored")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored")
    monkeypatch.setenv("POSTGRES_HOST", "ignored")
    monkeypatch.setenv("POSTGRES_DB", "ignored")
    s = _fresh_settings()
    assert s.database_url == "sqlite:///./explicit.sqlite"


def test_builds_postgres_url_from_discrete_vars(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "timehub")
    monkeypatch.setenv("POSTGRES_PASSWORD", "simple")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "timehub")
    s = _fresh_settings()
    assert s.database_url == "postgresql+psycopg://timehub:simple@db:5432/timehub"


def test_wild_password_gets_url_escaped(monkeypatch):
    # The actual regression: these are exactly the chars that broke
    # string-interpolated DATABASE_URLs before this change.
    monkeypatch.setenv("POSTGRES_USER", "timehub")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:wo/rd#$%")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_DB", "timehub")
    s = _fresh_settings()
    # The library escapes the password; it must NOT appear literally.
    assert "p@ss:wo/rd#$%" not in s.database_url
    # Host parses cleanly (no part of the password leaked into authority).
    assert "@db:5432/timehub" in s.database_url

    # Round-trip: SQLAlchemy can parse what it produced and recover the
    # original password.
    from sqlalchemy.engine import make_url

    url = make_url(s.database_url)
    assert url.host == "db"
    assert url.username == "timehub"
    assert url.password == "p@ss:wo/rd#$%"
