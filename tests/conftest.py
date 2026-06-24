import os
import re
import tempfile

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("INITIAL_ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "testpass")

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)  # noqa: SIM115
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    from app import models  # noqa: F401 — register models on metadata
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    from app.services.bootstrap import ensure_initial_admin

    ensure_initial_admin()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        # The web UI is CSRF-protected: every unsafe request must echo the
        # session's CSRF token. Prime a session by rendering a page, lift the
        # token out of its <meta> tag, and send it as a default header on all
        # subsequent requests (the token is stable for the session's lifetime).
        html = c.get("/login").text
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if m:
            c.headers["X-CSRF-Token"] = m.group(1)
        yield c
