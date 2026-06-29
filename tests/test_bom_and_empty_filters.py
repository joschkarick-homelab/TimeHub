"""Regressions surfaced by a real Toggl export:

- File starts with a UTF-8 BOM and the first column is quoted, which the
  csv module preserves verbatim. The mapping then misses, so even an
  unrelated column like ``Start date`` was reported as "entry_date missing"
  (the failure came from an *earlier* mismatched first key throwing off
  subsequent dictionary lookups in edge cases).
- The dashboard's export form posts ``project_id=`` when no project filter
  is set, which FastAPI's int parser rejects.
"""

import io
from datetime import date


def _login_session(client) -> None:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")


def _login_api(client) -> str:
    from tests.conftest import act_as

    act_as(client, "admin@example.com")
    return "hub-identity"


def test_import_handles_utf8_bom_in_headers(client):
    """A BOM-prefixed file with a quoted first column should still import
    cleanly — the headers in the mapping match after BOM/whitespace stripping."""
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}

    client.post(
        "/api/v1/projects",
        json={"name": "Toggl test", "code": "TOGGL", "default_sync_target": "intern"},
        headers=h,
    )

    fmt_id = client.post(
        "/api/v1/import-formats",
        json={
            "name": "Toggl-with-BOM",
            "separator": ",",
            "date_format": "%Y-%m-%d",
            "time_format": "%H:%M:%S",
            "column_map": {
                "description": "Description",
                "project_code": "Project",
                "entry_date": "Start date",
                "start_time": "Start time",
                "end_time": "Stop time",
            },
        },
        headers=h,
    ).json()["id"]

    # Exactly the shape Toggl exports: leading BOM, first column quoted.
    csv_bytes = (
        b"\xef\xbb\xbf"
        b'"Description",Duration,Project,Start date,Start time,Stop date,Stop time\n'
        b'"Implementierung",1:30:00,TOGGL,2026-05-22,10:30:00,2026-05-22,12:00:00\n'
        b'"Refactor",2:00:00,TOGGL,2026-05-23,09:00:00,2026-05-23,11:00:00\n'
    )

    files = {"file": ("toggl.csv", io.BytesIO(csv_bytes))}
    r = client.post(f"/api/v1/import-formats/{fmt_id}/run", files=files, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    # Without the BOM fix this would be 0 created / 2 failed with
    # "entry_date missing".
    assert body["created"] == 2, body
    assert body["failed"] == 0, body["errors"]


def test_ai_mapping_strips_bom_from_keys():
    """The AI saw a sample with BOM and dutifully returned a key with BOM.
    Our sanitizer should clean it so the saved format matches the importer."""
    from app.services.ai_mapping import _sanitize

    raw_ai = {
        "source_hint": "toggl",
        "separator": ",",
        "date_format": "%Y-%m-%d",
        "time_format": "%H:%M:%S",
        "column_map": {
            "﻿Description": "description",  # BOM'd first column
            "  Start date  ": "entry_date",      # padded
        },
    }
    s = _sanitize(raw_ai, "﻿Description,Start date\nx,2026-01-01\n")
    # column_map is target-keyed; the cleaned source headers are the values
    assert "Description" in s.column_map.values()
    assert "Start date" in s.column_map.values()
    # BOM-prefixed source got cleaned, not preserved
    assert "﻿Description" not in s.column_map.values()


def test_export_endpoint_tolerates_empty_project_id(client):
    """The dashboard renders an empty hidden ``project_id=`` when no project
    filter is active — that must not 422."""
    _login_session(client)
    token = _login_api(client)
    h = {"Authorization": f"Bearer {token}"}

    # need a format to export against
    fmt_id = client.post(
        "/api/v1/import-formats",
        json={
            "name": "EmptyPidFmt",
            "separator": ",",
            "date_format": "%Y-%m-%d",
            "column_map": {"entry_date": "Date", "duration_hours": "Hours",
                           "project_code": "Project"},
        },
        headers=h,
    ).json()["id"]

    today = date.today().isoformat()
    r = client.get(
        f"/entries/export?format_id={fmt_id}&date_from={today}&date_to={today}&project_id="
    )
    # Now 200 (export succeeds, possibly with zero rows), not 422.
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")


def test_index_tolerates_empty_project_id_querystring(client):
    """Same shape as the export bug, but on the dashboard's filter form."""
    _login_session(client)
    r = client.get("/?date_from=&date_to=&project_id=")
    assert r.status_code == 200
