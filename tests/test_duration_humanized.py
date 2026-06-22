"""Jira-style humanized duration format "1w 2d 3h 4m" for import and export.

Work-time units: 1 week = 5 days, 1 day = 8 hours (so 1w = 2400 min, 1d = 480 min).
"""

import pytest


def _login_session(client) -> None:
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


def _token(client) -> str:
    return client.post("/api/v1/auth/login",
                       json={"email": "admin@example.com", "password": "testpass"}).json()["access_token"]


# ---------- parsing ----------

@pytest.mark.parametrize("text,minutes", [
    ("1w 2d 3h 4m", 2400 + 960 + 180 + 4),  # 3544
    ("1w", 2400),
    ("2d", 960),
    ("3h", 180),
    ("4m", 4),
    ("2d 4h", 960 + 240),
    ("90m", 90),
    ("1,5h", 90),            # decimal token
    ("1.5h", 90),
    ("1w2d3h4m", 3544),      # tokens may run together
    ("  3h 30m  ", 210),     # surrounding whitespace tolerated
    ("1H 2M", 62),           # case-insensitive
])
def test_humanized_to_minutes(text, minutes):
    from app.services.transforms import humanized_duration_to_minutes
    assert humanized_duration_to_minutes(text) == minutes


@pytest.mark.parametrize("text", ["", "   ", "90", "01:30", "1,5", "1h banana 2m", "kaputt"])
def test_humanized_to_minutes_rejects(text):
    from app.services.transforms import humanized_duration_to_minutes
    assert humanized_duration_to_minutes(text) is None


def test_auto_duration_accepts_humanized():
    from app.services.transforms import auto_duration_to_minutes
    assert auto_duration_to_minutes("1w 2d 3h 4m") == 3544
    assert auto_duration_to_minutes("90m") == 90
    assert auto_duration_to_minutes("1,5h") == 90   # unit letter beats bare-decimal path
    # untouched: the pre-existing formats still resolve the same way
    assert auto_duration_to_minutes("01:30:00") == 90
    assert auto_duration_to_minutes("1,5") == 90
    assert auto_duration_to_minutes("90") == 90


# ---------- formatting ----------

@pytest.mark.parametrize("minutes,text", [
    (3544, "1w 2d 3h 4m"),
    (2400, "1w"),
    (480, "1d"),
    (90, "1h 30m"),
    (4, "4m"),
    (0, "0m"),
    (-5, "0m"),
    (960 + 240, "2d 4h"),
])
def test_minutes_to_humanized(minutes, text):
    from app.services.transforms import minutes_to_humanized
    assert minutes_to_humanized(minutes) == text


def test_humanized_round_trip():
    from app.services.transforms import humanized_duration_to_minutes, minutes_to_humanized
    for minutes in (1, 4, 90, 480, 2400, 3544, 12345):
        assert humanized_duration_to_minutes(minutes_to_humanized(minutes)) == minutes


# ---------- import ----------

def test_import_duration_human_target(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "HumanDur", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "project_code": "Project", "duration_human": "Dur"},
    }, headers=h).json()["id"]
    csv = ("Date,Project,Dur\n"
           "2026-05-01,HUMANDUR,1h 30m\n"
           "2026-05-02,HUMANDUR,2d\n"
           "2026-05-03,HUMANDUR,1w 2d 3h 4m\n")
    r = client.post(f"/api/v1/import-formats/{fid}/run",
                    files={"file": ("a.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201 and r.json()["created"] == 3, r.json()
    entries = client.get("/api/v1/time-entries", headers=h).json()
    mins = {e["entry_date"]: e["duration_minutes"] for e in entries
            if e["entry_date"] in ("2026-05-01", "2026-05-02", "2026-05-03")}
    assert mins == {"2026-05-01": 90, "2026-05-02": 960, "2026-05-03": 3544}


# ---------- export ----------

def test_export_duration_human_field():
    from datetime import date

    from app.services.reports import _format_value

    class _Entry:
        duration_minutes = 3544
        entry_date = date(2026, 5, 1)
        start_time = end_time = None
        description = ""
        tags = []
        sync_target_override = None
        external_ref = None
        sync_metadata_override = None

    class _Project:
        code = "P"
        default_sync_target = "intern"

    assert _format_value("duration_human", _Entry(), _Project(), None,
                         "%Y-%m-%d", "%H:%M") == "1w 2d 3h 4m"
