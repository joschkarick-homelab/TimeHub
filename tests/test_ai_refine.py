"""Phase 3: AI proposes transforms + target_rules, and the refine chat lets the
user iterate. The real Anthropic call is never made in tests; we exercise the
pure sanitizer/message-builder and stub suggest_mapping for the web flow."""


def _login_session(client) -> None:
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


# ---------- sanitizer keeps AI-proposed transforms/rules ----------

def test_sanitize_includes_transforms_and_rules():
    from app.services.ai_mapping import _sanitize
    raw = {
        "source_hint": "toggl", "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map": {"Description": "description"},
        "transforms": [
            {"target": "sync:jira.issue_key", "op": "regex", "source": "Description",
             "pattern": r"([A-Z]+-\d+)", "group": 1},
            {"target": "bogus_target", "op": "copy", "source": "X"},  # dropped
        ],
        "target_rules": [
            {"when": "sync:jira.issue_key", "set_target": "jira"},
            {"when": "sync:jira.issue_key", "set_target": "nope"},  # dropped
        ],
    }
    sug = _sanitize(raw, "Description\nTicket ABC-1: x\n")
    assert len(sug.transforms) == 1 and sug.transforms[0]["target"] == "sync:jira.issue_key"
    assert sug.target_rules == [{"when": "sync:jira.issue_key", "set_target": "jira"}]


# ---------- refine message construction ----------

def test_build_messages_initial_vs_refine():
    from app.services.ai_mapping import _build_messages
    initial = _build_messages("a,b\n1,2", 15, None, None)
    assert len(initial) == 1 and initial[0]["role"] == "user"

    prev = {"column_map": {"a": "description"}}
    refine = _build_messages("a,b\n1,2", 15, "Tickets aus b ziehen", prev)
    assert [m["role"] for m in refine] == ["user", "assistant", "user"]
    assert "Tickets aus b ziehen" in refine[2]["content"]
    assert "description" in refine[1]["content"]


def test_full_system_prompt_lists_sync_targets():
    from app.services.ai_mapping import _full_system_prompt
    p = _full_system_prompt()
    assert "sync:jira.issue_key" in p
    assert "target_rules" in p and "transforms" in p


# ---------- web wizard: AI suggestion + refine chat (stubbed AI) ----------

def _suggestion(**over):
    from app.schemas.import_format import ImportFormatSuggestion
    base = dict(
        source_hint="toggl", separator=",", encoding="utf-8",
        date_format="%Y-%m-%d", time_format="%H:%M",
        column_map={"description": "Description", "entry_date": "Date", "duration_hours": "Hours"},
        transforms=[], target_rules=[], default_project_code=None, notes="",
        detected_headers=["Date", "Hours", "Description"],
    )
    base.update(over)
    return ImportFormatSuggestion(**base)


_SAMPLE = "Date,Hours,Description\n2026-05-27,1.5,Ticket ABC-1: did things\n"


def test_new_format_review_shows_ai_transforms(client, monkeypatch):
    _login_session(client)
    import app.web.routes.formats as router
    monkeypatch.setattr(router, "suggest_mapping", lambda text, **kw: _suggestion(
        transforms=[{"target": "sync:jira.issue_key", "op": "regex", "source": "Description",
                     "pattern": r"([A-Z]+-\d+)", "group": 1}],
        target_rules=[{"when": "sync:jira.issue_key", "set_target": "jira"}],
        notes="Ticket aus Beschreibung gezogen.",
    ))
    r = client.post("/import-formats/new",
                    data={"name": "Toggl X"},
                    files={"sample": ("s.csv", _SAMPLE, "text/csv")})
    assert r.status_code == 200
    # AI-proposed transform + rule are embedded for the editor to render
    assert "sync:jira.issue_key" in r.text
    assert "Ticket aus Beschreibung gezogen." in r.text
    # the sample is carried for the refine turn
    assert "Ticket ABC-1: did things" in r.text


def test_refine_passes_instruction_and_previous(client, monkeypatch):
    _login_session(client)
    import json

    import app.web.routes.formats as router
    captured = {}

    def fake_suggest(text, *, instruction=None, previous=None, hints=None):
        captured["text"] = text
        captured["instruction"] = instruction
        captured["previous"] = previous
        return _suggestion(
            transforms=[{"target": "sync:jira.issue_key", "op": "regex",
                         "source": "Description", "pattern": r"([A-Z]+-\d+)"}],
            target_rules=[{"when": "sync:jira.issue_key", "set_target": "jira"}],
        )

    monkeypatch.setattr(router, "suggest_mapping", fake_suggest)
    r = client.post("/import-formats/refine", data={
        "name": "Toggl X", "sample_text": _SAMPLE,
        "instruction": "Jira-Tickets aus der Beschreibung als sync:jira.issue_key ziehen",
        "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": json.dumps({"description": "Description"}),
        "transforms_json": "[]", "target_rules_json": "[]",
    })
    assert r.status_code == 200
    assert "sync:jira.issue_key" in captured["instruction"]
    assert captured["text"] == _SAMPLE
    # previous reflects the current state we submitted
    assert captured["previous"]["column_map"] == {"Description": "description"}
    # the refined suggestion is rendered
    assert "sync:jira.issue_key" in r.text


def test_refine_without_instruction_warns_and_keeps_state(client, monkeypatch):
    _login_session(client)
    import json

    import app.web.routes.formats as router
    called = {"n": 0}
    monkeypatch.setattr(router, "suggest_mapping",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _suggestion())
    r = client.post("/import-formats/refine", data={
        "name": "Toggl X", "sample_text": _SAMPLE, "instruction": "   ",
        "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": json.dumps({"description": "Description"}),
        "transforms_json": "[]", "target_rules_json": "[]",
    })
    assert r.status_code == 200
    assert called["n"] == 0  # no AI call without an instruction
    assert "Bitte eine Anweisung" in r.text


def test_refine_handles_ai_error_without_losing_state(client, monkeypatch):
    _login_session(client)
    import json

    import app.web.routes.formats as router
    from app.services.ai_mapping import AiMappingError

    def boom(*a, **k):
        raise AiMappingError("AI kaputt")
    monkeypatch.setattr(router, "suggest_mapping", boom)
    r = client.post("/import-formats/refine", data={
        "name": "Toggl X", "sample_text": _SAMPLE, "instruction": "tu was",
        "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": json.dumps({"description": "Description"}),
        "transforms_json": json.dumps([{"target": "sync:jira.issue_key", "op": "copy",
                                        "source": "Description"}]),
        "target_rules_json": "[]",
    })
    assert r.status_code == 200
    assert "AI kaputt" in r.text
    # the transform we already had is still present after the error
    assert "sync:jira.issue_key" in r.text
