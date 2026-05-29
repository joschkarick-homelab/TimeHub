"""The AI mapping result must drive the review screen — headers from the AI
suggestion show up in detected_headers, even if our own parser missed them."""

from unittest.mock import MagicMock, patch

from app.services.ai_mapping import _sanitize, suggest_mapping


def test_sanitize_unions_ai_columns_with_detected_headers():
    raw_ai = {
        "source_hint": "toggl",
        "separator": ",",
        "encoding": "utf-8",
        "date_format": "%Y-%m-%d",
        "time_format": "%H:%M:%S",
        "column_map": {
            "Start date": "entry_date",
            "Duration": "duration_minutes",
            "Project": "project_code",
            "MissingFromHeader": "description",  # not in the sample's header row
        },
        "default_project_code": None,
        "notes": "Toggl-Export erkannt.",
    }
    sample = "Start date,Project,Duration\n2026-05-27,DEMO,90\n"
    s = _sanitize(raw_ai, sample)
    # Real headers from the CSV
    assert "Start date" in s.detected_headers
    assert "Project" in s.detected_headers
    assert "Duration" in s.detected_headers
    # Header that only the AI mentioned still shows up so the user can override
    assert "MissingFromHeader" in s.detected_headers
    # column_map is target-keyed ({target: source}) after sanitizing
    assert s.column_map["entry_date"] == "Start date"


def test_sanitize_drops_unknown_target_fields():
    raw_ai = {
        "source_hint": "x",
        "separator": ";",
        "encoding": "utf-8",
        "date_format": "%d.%m.%Y",
        "time_format": "%H:%M",
        "column_map": {
            "A": "entry_date",
            "B": "totally_made_up_field",  # not in SUPPORTED_TARGETS
        },
    }
    s = _sanitize(raw_ai, "A;B\n01.01.2026;x\n")
    assert s.column_map == {"entry_date": "A"}


def test_suggest_mapping_requires_api_key(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: type("S", (), {
        "anthropic_api_key": None,
        "ai_mapping_max_sample_lines": 15,
        "ai_mapping_model": "x",
    })())
    from app.services.ai_mapping import AiMappingError

    try:
        suggest_mapping("a,b\n1,2\n")
    except AiMappingError as e:
        assert "ANTHROPIC_API_KEY" in str(e)
    else:
        raise AssertionError("expected AiMappingError")
