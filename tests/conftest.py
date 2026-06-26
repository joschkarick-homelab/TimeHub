import os
import re
import tempfile

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ["AUTH_MODE"] = "hub"            # headers are the identity source in tests
os.environ["ADMIN_EMAILS"] = "admin@example.com"
os.environ.setdefault("SECRET_KEY", "test-secret")

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)  # noqa: SIM115
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"


def _refresh_csrf(client) -> None:
    """Render a page to mint/refresh the session CSRF token and set it as a
    default header for subsequent unsafe requests."""
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if m:
        client.headers["X-CSRF-Token"] = m.group(1)


def hub_headers(email, *, msq_user_id=None, name=None, roles=""):
    """X-MSQ-* header dict for a per-request Hub identity (auto-provisioned on
    first use). Use to authorize the JSON API as a specific user without
    changing the client's default identity — handy for tests that interleave
    several users."""
    return {
        "X-MSQ-User-Id": msq_user_id or f"msq-{email}",
        "X-MSQ-User-Email": email,
        "X-MSQ-User-Name": name or email,
        "X-MSQ-Roles": roles,
    }


def act_as(client, email, *, msq_user_id=None, name=None, roles=""):
    """Make subsequent requests act as this Hub user (auto-provisioned on first use)."""
    client.headers["X-MSQ-User-Id"] = msq_user_id or f"msq-{email}"
    client.headers["X-MSQ-User-Email"] = email
    client.headers["X-MSQ-User-Name"] = name or email
    client.headers["X-MSQ-Roles"] = roles
    _refresh_csrf(client)


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    from app import models  # noqa: F401 — register models on metadata
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    from app.services.bootstrap import ensure_builtin_formats

    ensure_builtin_formats()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        # Carry a DEFAULT admin Hub identity via X-MSQ-* headers, plus the
        # session CSRF token (every unsafe web request must echo it).
        c.headers["X-MSQ-User-Id"] = "admin-msq"
        c.headers["X-MSQ-User-Email"] = "admin@example.com"
        c.headers["X-MSQ-User-Name"] = "Admin"
        _refresh_csrf(c)
        yield c


@pytest.fixture
def raw_client():
    """A TestClient with NO X-MSQ headers — for unauthenticated behavior and
    API-key-only auth (where the Hub identity must not be resolved first)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
