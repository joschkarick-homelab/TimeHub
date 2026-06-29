"""Regression tests for the Medium-finding fixes (M4, M5, M8, M9)."""


# ── M5: duration transform accepts any format ────────────────────────────────

def test_duration_transform_accepts_non_clock_formats():
    from app.services.transforms import apply_transform

    rule = {"op": "duration", "source": "Dauer", "target": "duration_minutes"}
    assert apply_transform(rule, {"Dauer": "90"}) == "90"
    assert apply_transform(rule, {"Dauer": "1,5"}) == "90"      # decimal hours
    assert apply_transform(rule, {"Dauer": "1:30"}) == "90"     # clock still works
    assert apply_transform(rule, {"Dauer": "1h 30m"}) == "90"   # humanized


def test_duration_transform_to_hours_target():
    from app.services.transforms import apply_transform

    rule = {"op": "duration", "source": "D", "target": "duration_hours"}
    assert apply_transform(rule, {"D": "90"}) == "1.5"


# ── M4: Salesforce duration snapping is surfaced, not silent ──────────────────

def test_duration_snap_warning_only_when_changed():
    from app.services.salesforce import duration_snap_warning, snapped_total_minutes

    assert duration_snap_warning(90) is None            # already on the grid
    assert duration_snap_warning(97) is not None        # rounded off the grid
    assert snapped_total_minutes(97) == 90              # nearest 15-min slot
    assert snapped_total_minutes(98) == 105             # rounds up
    # Capped at 23:45 for absurdly long entries.
    assert snapped_total_minutes(24 * 60) == 23 * 60 + 45
    assert "gedeckelt" in duration_snap_warning(24 * 60)


# ── M8: JWT via PyJWT round-trips and rejects tampering ───────────────────────

def test_jwt_roundtrip_and_invalid_token():
    import jwt
    import pytest

    from app.config import get_settings
    from app.security import ALGORITHM, decode_token

    # decode_token is still used by the MCP bearer path; prove it round-trips a
    # well-formed token and rejects a tampered one. (Issuing is no longer an
    # app concern after the login cutover, so we mint the token directly here.)
    tok = jwt.encode({"sub": "42"}, get_settings().secret_key, algorithm=ALGORITHM)
    assert decode_token(tok)["sub"] == "42"
    with pytest.raises(ValueError):
        decode_token(tok + "tampered")
