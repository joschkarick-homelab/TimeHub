"""CSV import robustness (H6): defensive decoding and night-shift handling."""


def test_decode_csv_bytes_falls_back_to_cp1252():
    from app.services.csv_import import _decode_csv_bytes

    # German Excel often exports cp1252; 0xFC ('ü') is invalid UTF-8.
    raw = "Tätigkeit".encode("cp1252")
    assert _decode_csv_bytes(raw, "utf-8") == "Tätigkeit"


def test_decode_csv_bytes_prefers_given_encoding():
    from app.services.csv_import import _decode_csv_bytes

    assert _decode_csv_bytes("Tätigkeit".encode(), "utf-8") == "Tätigkeit"


def test_decode_csv_bytes_last_resort_replaces_undecodable():
    from app.services.csv_import import _decode_csv_bytes

    # Undecodable in cp1252 too (0x81 is unmapped) → replaced, never raises.
    out = _decode_csv_bytes(b"ok\x81", "utf-8")
    assert out.startswith("ok")


def test_import_handles_night_shift_over_midnight(client):
    # `client` fixture ensures the app/DB and the bootstrap admin (id 1) exist.
    from app.db import SessionLocal
    from app.services.csv_import import import_csv

    csv_bytes = b"Datum;Start;Ende\n2026-06-01;22:00;01:00\n"
    db = SessionLocal()
    try:
        res = import_csv(
            db, user_id=1, raw_bytes=csv_bytes,
            column_map={"entry_date": "Datum", "start_time": "Start", "end_time": "Ende"},
            separator=";", default_project_code="NIGHT",
        )
        # 22:00 → 01:00 must import as a 180-minute entry, not be rejected as
        # a negative duration.
        assert res["created"] == 1, res["errors"]
        from app.models import TimeEntry

        entry = db.get(TimeEntry, res["ids"][0])
        assert entry.duration_minutes == 180
    finally:
        db.close()
