"""Ready-to-upload Jira worklog export.

Covers the engine fixes (locale-independent English month, a real time of day
instead of midnight, humanized Timespent), the seeded global "Export für Jira"
format, the target ("Ziel") filter on the dashboard + export endpoint, and the
download-overlay cookie.
"""

from datetime import date, time
from types import SimpleNamespace

from app.services.bootstrap import _JIRA_EXPORT_SPEC, JIRA_EXPORT_FORMAT_NAME
from app.services.reports import _strftime_en, export_via_import_format


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def _token(client) -> str:
    return client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "testpass"},
    ).json()["access_token"]


def _project(client, code: str, target: str) -> int:
    h = {"Authorization": f"Bearer {_token(client)}"}
    r = client.post(
        "/api/v1/projects",
        json={"name": code.title(), "code": code, "default_sync_target": target},
        headers=h,
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    return next(
        p["id"] for p in client.get("/api/v1/projects", headers=h).json() if p["code"] == code
    )


def _entry(client, project_id: int, **extra) -> dict:
    h = {"Authorization": f"Bearer {_token(client)}"}
    payload = {"project_id": project_id, "entry_date": "2026-03-10", "duration_minutes": 90}
    payload.update(extra)
    r = client.post("/api/v1/time-entries", json=payload, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- engine-level unit tests ----------

def test_strftime_en_forces_english_month():
    from datetime import datetime

    dt = datetime(2026, 3, 1, 9, 10, 0)
    # March is "Mär" in a German locale — must stay English here.
    assert _strftime_en(dt, "%d-%b-%Y %H:%M:%S") == "01-Mar-2026 09:10:00"
    assert _strftime_en(dt, "%d.%m.%Y") == "01.03.2026"  # no month token, unchanged
    assert _strftime_en(dt, "100%% %b") == "100% Mar"  # literal percent preserved


def test_export_renders_real_time_and_english_month():
    entry = SimpleNamespace(
        entry_date=date(2026, 3, 10),
        start_time=time(9, 10),
        duration_minutes=90,
        description="Ticket-Arbeit",
        sync_metadata_override={"jira": {"issue_key": "ABC-123"}},
    )
    body, _ = export_via_import_format(
        [(entry, SimpleNamespace(), SimpleNamespace())],
        _JIRA_EXPORT_SPEC["column_map"],
        separator=_JIRA_EXPORT_SPEC["separator"],
        date_format=_JIRA_EXPORT_SPEC["date_format"],
    )
    lines = body.strip().splitlines()
    assert lines[0] == "Ticket No,Start Date,Timespent,Comment"
    assert lines[1] == "ABC-123,10-Mar-2026 09:10:00,1h 30m,Ticket-Arbeit"


def test_export_defaults_time_when_no_start_time():
    entry = SimpleNamespace(
        entry_date=date(2026, 3, 10),
        start_time=None,
        duration_minutes=30,
        description="x",
        sync_metadata_override={"jira": {"issue_key": "ABC-1"}},
    )
    body, _ = export_via_import_format(
        [(entry, SimpleNamespace(), SimpleNamespace())],
        _JIRA_EXPORT_SPEC["column_map"],
        date_format=_JIRA_EXPORT_SPEC["date_format"],
        separator=",",
    )
    # 09:00 default avoids a midnight timezone day-shift; 30 min → "30m".
    assert "10-Mar-2026 09:00:00,30m," in body


# ---------- end-to-end web tests ----------

def test_builtin_jira_format_is_seeded_globally(client):
    h = {"Authorization": f"Bearer {_token(client)}"}
    formats = client.get("/api/v1/import-formats", headers=h).json()
    jira = [f for f in formats if f["name"] == JIRA_EXPORT_FORMAT_NAME]
    assert len(jira) == 1, "exactly one global Jira format expected"
    assert jira[0]["is_global"] is True
    assert jira[0]["date_format"] == "%d-%b-%Y %H:%M:%S"


def test_export_target_filter_excludes_other_targets(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}

    jira_pid = _project(client, "JIRAEXP", "jira")
    _entry(
        client, jira_pid, description="jira-only-row", start_time="09:10",
        sync_metadata_override={"jira": {"issue_key": "ABC-777"}},
    )
    sf_pid = _project(client, "SFEXP", "salesforce")
    _entry(client, sf_pid, description="salesforce-only-row")

    fmt = next(
        f for f in client.get("/api/v1/import-formats", headers=h).json()
        if f["name"] == JIRA_EXPORT_FORMAT_NAME
    )
    r = client.get(f"/entries/export?format_id={fmt['id']}&target=jira")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.text

    assert body.splitlines()[0] == "Ticket No,Start Date,Timespent,Comment"
    assert "ABC-777" in body
    assert "10-Mar-2026 09:10:00" in body
    assert "1h 30m" in body
    assert "jira-only-row" in body
    # The Salesforce-only entry must not leak into a Jira export.
    assert "salesforce-only-row" not in body


def test_export_sets_download_cookie(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fmt = next(
        f for f in client.get("/api/v1/import-formats", headers=h).json()
        if f["name"] == JIRA_EXPORT_FORMAT_NAME
    )
    r = client.get(f"/entries/export?format_id={fmt['id']}&dl_token=tok123")
    assert r.status_code == 200
    assert "th_dl=tok123" in r.headers.get("set-cookie", "")


def test_dashboard_renders_target_filter(client):
    _login_session(client)
    r = client.get("/")
    assert r.status_code == 200
    # The new "Ziel" dropdown is present with the target options.
    assert 'name="target"' in r.text
    assert "Jira" in r.text
