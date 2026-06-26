"""Microsoft 365 calendar integration: config store, PKCE/authorize URL,
event projection onto days, OAuth callback hardening and the calendar overlay."""

import base64
import hashlib
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.services import m365 as m365_svc


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _admin(db):
    from sqlalchemy import select

    from app.models import User

    return db.execute(select(User).where(User.email == "admin@example.com")).scalar_one()


# ── config store ────────────────────────────────────────────────────────────

def test_config_roundtrip_encrypts_secret(db):
    m365_svc.save_config(
        db,
        client_id="cid-123",
        tenant="contoso.onmicrosoft.com",
        client_secret="s3cr3t-value",
        timezone="Europe/Berlin",
    )
    cfg = m365_svc.get_config(db)
    assert cfg["client_id"] == "cid-123"
    assert cfg["tenant"] == "contoso.onmicrosoft.com"
    assert cfg["client_secret"] == "s3cr3t-value"  # decrypted back
    assert m365_svc.configured(db) is True

    # The stored secret is encrypted at rest, never plaintext.
    from app.services import app_settings as app_settings_svc

    stored = app_settings_svc.get_setting(db, m365_svc.M365_CLIENT_SECRET_KEY, "")
    assert stored.startswith("enc:1:")
    assert "s3cr3t-value" not in stored


def test_config_empty_secret_keeps_existing(db):
    m365_svc.save_config(db, client_id="cid", client_secret="keep-me")
    m365_svc.save_config(db, client_id="cid2", client_secret="")  # blank → unchanged
    assert m365_svc.get_config(db)["client_secret"] == "keep-me"


def test_tenant_defaults_to_organizations(db):
    m365_svc.save_config(db, client_id="x", client_secret="y", tenant="")
    assert m365_svc.get_config(db)["tenant"] == "organizations"


# ── PKCE + authorize URL ─────────────────────────────────────────────────────

def test_make_pkce_is_valid_s256():
    verifier, challenge = m365_svc.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected


def test_authorize_url_carries_params(db):
    m365_svc.save_config(db, client_id="abc", client_secret="sec", tenant="organizations")
    url = m365_svc.authorize_url(
        db, state="st8", code_challenge="chal", redirect_uri="https://h/m365/callback"
    )
    parsed = urlparse(url)
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path == "/organizations/oauth2/v2.0/authorize"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["abc"]
    assert q["state"] == ["st8"]
    assert q["code_challenge"] == ["chal"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["https://h/m365/callback"]
    assert "Calendars.Read" in q["scope"][0]


# ── event projection ─────────────────────────────────────────────────────────

def _ev(subject, start, end, all_day=False, show_as="busy", organizer=""):
    return {
        "subject": subject,
        "start_dt": start,
        "end_dt": end,
        "all_day": all_day,
        "show_as": show_as,
        "organizer": organizer,
    }


def test_events_for_day_timed_block():
    from datetime import date

    events = [_ev("Standup", datetime(2026, 6, 29, 9, 0), datetime(2026, 6, 29, 9, 30))]
    out = m365_svc.events_for_day(events, date(2026, 6, 29))
    assert out["timed"] == [
        {
            "subject": "Standup",
            "start": 540,
            "end": 570,
            "show_as": "busy",
            "organizer": "",
            "continued": False,
            "continues": False,
        }
    ]


def test_events_for_day_excludes_other_days():
    from datetime import date

    events = [_ev("X", datetime(2026, 6, 29, 9, 0), datetime(2026, 6, 29, 10, 0))]
    assert m365_svc.events_for_day(events, date(2026, 6, 30))["timed"] == []


def test_events_for_day_clamps_midnight_span():
    from datetime import date

    # 23:00 Mon → 01:00 Tue: clamped per day, flagged continues/continued.
    events = [_ev("Late", datetime(2026, 6, 29, 23, 0), datetime(2026, 6, 30, 1, 0))]
    mon = m365_svc.events_for_day(events, date(2026, 6, 29))["timed"][0]
    assert mon["start"] == 1380 and mon["end"] == 1440 and mon["continues"] is True
    tue = m365_svc.events_for_day(events, date(2026, 6, 30))["timed"][0]
    assert tue["start"] == 0 and tue["end"] == 60 and tue["continued"] is True


def test_events_for_day_all_day_goes_to_allday():
    from datetime import date

    events = [_ev("Urlaub", datetime(2026, 6, 29, 0, 0), datetime(2026, 6, 30, 0, 0), all_day=True)]
    out = m365_svc.events_for_day(events, date(2026, 6, 29))
    assert out["timed"] == []
    assert out["allday"] == [{"subject": "Urlaub", "show_as": "busy"}]


def test_normalize_event_parses_graph_payload():
    raw = {
        "subject": "Call",
        "start": {"dateTime": "2026-06-29T14:00:00.0000000", "timeZone": "Europe/Berlin"},
        "end": {"dateTime": "2026-06-29T15:00:00.0000000", "timeZone": "Europe/Berlin"},
        "isAllDay": False,
        "showAs": "busy",
        "organizer": {"emailAddress": {"name": "Jane Doe"}},
    }
    norm = m365_svc._normalize_event(raw)
    assert norm["subject"] == "Call"
    assert norm["start_dt"] == datetime(2026, 6, 29, 14, 0)
    assert norm["end_dt"] == datetime(2026, 6, 29, 15, 0)
    assert norm["organizer"] == "Jane Doe"


# ── OAuth routes ─────────────────────────────────────────────────────────────

def test_callback_rejects_bad_state(client):
    _login_session(client)
    # No m365_oauth in session → state cannot match → redirect to profile error.
    r = client.get(
        "/m365/callback?code=abc&state=forged", follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/profile?error=")


def test_connect_requires_configuration(client, db):
    # Wipe any config a prior test set so "not configured" is the real state.
    from app.models import AppSetting

    for key in (m365_svc.M365_CLIENT_ID_KEY, m365_svc.M365_CLIENT_SECRET_KEY):
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()

    _login_session(client)
    r = client.get("/m365/connect", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/profile?error=")


# ── calendar overlay ─────────────────────────────────────────────────────────

@pytest.fixture
def admin_m365_connection(db):
    """Give the admin a connection row; clean it up so other suites that hit
    /calendar don't trip a (real) Graph fetch."""
    from app.models import M365Connection

    admin = _admin(db)
    conn = M365Connection(user_id=admin.id, account="admin@contoso.com")
    db.add(conn)
    db.commit()
    yield conn
    fresh = db.get(M365Connection, conn.id)
    if fresh is not None:
        db.delete(fresh)
        db.commit()


def test_calendar_renders_m365_lane(client, monkeypatch, admin_m365_connection):
    def fake_view(_db, _conn, start, end):
        return [_ev("Kundentermin", datetime(2026, 5, 27, 10, 0), datetime(2026, 5, 27, 11, 0))]

    monkeypatch.setattr(m365_svc, "calendar_view", fake_view)
    _login_session(client)
    r = client.get("/calendar?days=1&start=2026-05-27")
    assert r.status_code == 200
    assert "cal-m365-lane" in r.text
    assert "Kundentermin" in r.text


def test_calendar_m365_error_is_non_fatal(client, monkeypatch, admin_m365_connection):
    def boom(_db, _conn, start, end):
        raise m365_svc.M365Error("Graph kaputt")

    monkeypatch.setattr(m365_svc, "calendar_view", boom)
    _login_session(client)
    r = client.get("/calendar?days=1&start=2026-05-27")
    # Page still renders; the failure shows as a banner, tracking stays usable.
    assert r.status_code == 200
    assert "Graph kaputt" in r.text
    assert 'id="cal-scroll"' in r.text
