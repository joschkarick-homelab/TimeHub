import importlib
import re

from starlette.requests import Request

from app.web.common import redirect_to
from app.web.templating import join_base


def test_join_base_prefixes_root_path():
    assert join_base("/timehub", "/static/app.js") == "/timehub/static/app.js"
    assert join_base("", "/static/app.js") == "/static/app.js"
    assert join_base("/timehub", "static/app.js") == "/timehub/static/app.js"
    assert join_base("/timehub/", "/x") == "/timehub/x"


def test_health_endpoint_no_auth(raw_client):
    # /health must work with NO identity (raw_client has no X-MSQ headers).
    r = raw_client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_html_pages_carry_no_cache(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


def _request_with_root_path(root_path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [],
            "path": "/",
            "root_path": root_path,
        }
    )


def test_redirect_to_prefixes_app_relative_next_once():
    # `next` is app-relative (no slug); redirect_to must prefix exactly once.
    resp = redirect_to(_request_with_root_path("/timehub"), "/?date_from=2026-06-01")
    assert resp.headers["location"] == "/timehub/?date_from=2026-06-01"
    # No slug → unchanged (matches the test environment / bare deployment).
    resp0 = redirect_to(_request_with_root_path(""), "/?date_from=2026-06-01")
    assert resp0.headers["location"] == "/?date_from=2026-06-01"
    # Open-redirect fallback also gets prefixed.
    resp_fb = redirect_to(_request_with_root_path("/timehub"), "https://evil.test")
    assert resp_fb.headers["location"] == "/timehub/"


def test_entry_create_redirect_keeps_slug(monkeypatch):
    # Regression: with a real Hub mount path, an entry-create POST must redirect
    # back to a slug-prefixed `next`, never to the Hub root (no /timehub/timehub).
    monkeypatch.setenv("BASE_PATH", "/timehub")
    import app.config as cfg

    cfg.get_settings.cache_clear()
    import app.main as main

    importlib.reload(main)
    try:
        from fastapi.testclient import TestClient

        with TestClient(main.app) as c:
            c.headers.update(
                {
                    "X-MSQ-User-Id": "slug-admin",
                    "X-MSQ-User-Email": "admin@example.com",
                    "X-MSQ-User-Name": "Admin",
                }
            )
            m = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").text)
            assert m, "CSRF token meta not found"
            c.headers["X-CSRF-Token"] = m.group(1)

            # Need a project to attach the entry to.
            pr = c.post(
                "/projects",
                data={"code": "SLUG1", "name": "Slug Project"},
                follow_redirects=False,
            )
            assert pr.status_code in (302, 303)
            assert pr.headers["location"].startswith("/timehub/"), pr.headers["location"]

            projects_html = c.get("/projects").text
            project_id = int(re.search(r"projects/(\d+)/edit", projects_html).group(1))

            nxt = "/?date_from=2026-06-01"
            er = c.post(
                "/entries",
                data={
                    "entry_date": "2026-06-02",
                    "project_id": str(project_id),
                    "duration_minutes": "60",
                    "next": nxt,
                },
                follow_redirects=False,
            )
            assert er.status_code in (302, 303)
            loc = er.headers["location"]
            assert loc.startswith("/timehub/"), loc
            assert "/timehub/timehub" not in loc, f"double-prefixed: {loc}"
            assert loc == "/timehub/?date_from=2026-06-01", loc
    finally:
        monkeypatch.delenv("BASE_PATH", raising=False)
        cfg.get_settings.cache_clear()
        importlib.reload(main)
