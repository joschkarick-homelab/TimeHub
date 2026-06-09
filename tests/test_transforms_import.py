"""Phase 2: per-column transforms, sync-field import targets, conditional
target rules — pure engine + importer integration + web persistence + export."""

from types import SimpleNamespace


def _login_session(client) -> None:
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


def _token(client) -> str:
    return client.post("/api/v1/auth/login",
                       json={"email": "admin@example.com", "password": "testpass"}).json()["access_token"]


# ---------- transform engine (pure) ----------

def test_apply_transform_regex_group():
    from app.services.transforms import apply_transform
    rule = {"target": "x", "op": "regex", "source": "d", "pattern": r"([A-Z]+-\d+)", "group": 1}
    assert apply_transform(rule, {"d": "Ticket ABC-123: stuff"}) == "ABC-123"
    assert apply_transform(rule, {"d": "no ticket here"}) is None


def test_apply_transform_date_reformats_to_target_format():
    from app.services.transforms import apply_transform
    rule = {"target": "entry_date", "op": "date", "source": "d", "date_from": "%d.%m.%Y"}
    assert apply_transform(rule, {"d": "27.05.2026"}, date_format="%Y-%m-%d") == "2026-05-27"
    assert apply_transform(rule, {"d": "garbage"}, date_format="%Y-%m-%d") is None


def test_apply_transform_split_constant_default():
    from app.services.transforms import apply_transform
    assert apply_transform({"op": "split", "source": "s", "sep": ":", "index": 0},
                           {"s": "ABC-1: did things"}) == "ABC-1"
    assert apply_transform({"op": "constant", "value": "fixed"}, {}) == "fixed"
    # default kicks in when the primary result is empty
    assert apply_transform({"op": "regex", "source": "s", "pattern": r"(\d+)", "default": "0"},
                           {"s": "none"}) == "0"


def test_safe_search_handles_bad_pattern_and_is_bounded():
    from app.services.transforms import safe_search
    assert safe_search("(unclosed", "abc") is None  # invalid regex → None, no raise
    # long input must not raise and should still match a simple pattern quickly
    assert safe_search(r"x", "y" * 50000 + "x") is None  # truncated before the x → no match
    assert safe_search(r"y", "y" * 50000) is not None


def test_split_transform_lands_on_both_rows(client):
    """Reproduces a user report: "Split by : 1" on a Description like
    "Ticket 123: …" — the right-hand side should land for every row, even when
    the resulting value doesn't match Jira's strict pattern (then it's stored
    but flagged as a format warning in the readiness display)."""
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fmt = client.post("/api/v1/import-formats", json={
        "name": "SplitDesc", "separator": ",",
        "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "duration_hours": "Hours",
                       "project_code": "Project"},
        "transforms": [{"target": "sync:jira.issue_key", "op": "split",
                        "source": "Desc", "sep": ":", "index": 1}],
    }, headers=h).json()
    csv = ('Date,Hours,Project,Desc\n'
           '2026-04-10,1,SPLITP,"Ticket 123: Implementierung"\n'
           '2026-04-11,1,SPLITP,"Ticket 123: ABC-7"\n')
    r = client.post(f"/api/v1/import-formats/{fmt['id']}/run",
                    files={"file": ("d.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201 and r.json()["created"] == 2, r.json()
    entries = client.get("/api/v1/time-entries", headers=h).json()
    on_dates = {e["entry_date"]: e["sync_metadata_override"] for e in entries
                if e["entry_date"] in ("2026-04-10", "2026-04-11")}
    # Both rows landed — the freeform one just fails the Jira pattern (a UI
    # readiness check), but the value is stored either way.
    assert on_dates["2026-04-10"] == {"jira": {"issue_key": "Implementierung"}}
    assert on_dates["2026-04-11"] == {"jira": {"issue_key": "ABC-7"}}


def test_eval_target_rules():
    from app.services.transforms import eval_target_rules
    rules = [{"when": "sync:jira.issue_key", "set_target": "jira"}]
    assert eval_target_rules(rules, {}, {"jira": {"issue_key": "ABC-1"}}, {}) == "jira"
    assert eval_target_rules(rules, {}, {}, {}) is None
    src_rules = [{"when_source": "Desc", "pattern": r"[A-Z]+-\d+", "set_target": "jira"}]
    assert eval_target_rules(src_rules, {}, {}, {"Desc": "see ABC-9"}) == "jira"
    assert eval_target_rules(src_rules, {}, {}, {"Desc": "nothing"}) is None


# ---------- importer integration (via API) ----------

_CSV = (
    "Date,Hours,Project,Description\n"
    "2026-05-27,1.5,ACME,Ticket ABC-123: built thing\n"
    "2026-05-27,2,ACME,no ticket today\n"
)


def _make_format(client, **overrides) -> int:
    h = {"Authorization": f"Bearer {_token(client)}"}
    payload = {
        "name": overrides.pop("name", "Jira Regex"),
        "separator": ",",
        "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "duration_hours": "Hours",
                       "project_code": "Project", "description": "Description"},
        "transforms": [{"target": "sync:jira.issue_key", "op": "regex",
                        "source": "Description", "pattern": r"([A-Z]+-\d+)", "group": 1}],
        "target_rules": [{"when": "sync:jira.issue_key", "set_target": "jira"}],
    }
    payload.update(overrides)
    return client.post("/api/v1/import-formats", json=payload, headers=h).json()["id"]


def _run(client, fmt_id, apply_rules: bool):
    h = {"Authorization": f"Bearer {_token(client)}"}
    return client.post(
        f"/api/v1/import-formats/{fmt_id}/run?apply_target_rules={'true' if apply_rules else 'false'}",
        files={"file": ("t.csv", _CSV, "text/csv")},
        headers=h,
    )


def test_import_regex_routes_to_sync_field_and_sets_target(client):
    _login_session(client)
    fmt_id = _make_format(client)
    r = _run(client, fmt_id, apply_rules=True)
    assert r.status_code == 201
    h = {"Authorization": f"Bearer {_token(client)}"}
    entries = client.get("/api/v1/time-entries", headers=h).json()
    with_ticket = [e for e in entries if e["description"] == "Ticket ABC-123: built thing"][0]
    assert with_ticket["sync_metadata_override"] == {"jira": {"issue_key": "ABC-123"}}
    assert with_ticket["sync_target_override"] == "jira"
    # the row without a ticket gets neither
    without = [e for e in entries if e["description"] == "no ticket today"][0]
    assert without["sync_metadata_override"] == {}
    assert without["sync_target_override"] is None


def test_import_without_apply_rules_keeps_meta_but_no_target(client):
    _login_session(client)
    fmt_id = _make_format(client, name="Jira NoRules")
    r = _run(client, fmt_id, apply_rules=False)
    assert r.status_code == 201
    h = {"Authorization": f"Bearer {_token(client)}"}
    entries = client.get("/api/v1/time-entries", headers=h).json()
    e = [e for e in entries if e["description"] == "Ticket ABC-123: built thing"]
    assert e and e[0]["sync_metadata_override"] == {"jira": {"issue_key": "ABC-123"}}
    # target rule not applied → no override
    assert all(x["sync_target_override"] != "jira" or x["description"] != "no ticket today" for x in entries)


def test_import_date_transform(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fmt_id = client.post("/api/v1/import-formats", json={
        "name": "DE dates", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"duration_hours": "Hours", "project_code": "Project"},
        "transforms": [{"target": "entry_date", "op": "date", "source": "Tag", "date_from": "%d.%m.%Y"}],
    }, headers=h).json()["id"]
    csv_de = "Tag,Hours,Project\n01.04.2026,1,DEPROJ\n"
    r = client.post(f"/api/v1/import-formats/{fmt_id}/run",
                    files={"file": ("d.csv", csv_de, "text/csv")}, headers=h)
    assert r.status_code == 201 and r.json()["created"] == 1
    entries = client.get("/api/v1/time-entries", headers=h).json()
    assert any(e["entry_date"] == "2026-04-01" for e in entries)


# ---------- web persistence ----------

def test_web_format_save_persists_transforms(client):
    _login_session(client)
    import json
    r = client.post("/import-formats", data={
        "name": "WebTransforms", "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": json.dumps({"description": "Description"}),
        "transforms_json": json.dumps([{"target": "sync:jira.issue_key", "op": "regex",
                                        "source": "Description", "pattern": r"([A-Z]+-\d+)"}]),
        "target_rules_json": json.dumps([{"when": "sync:jira.issue_key", "set_target": "jira"}]),
    }, follow_redirects=False)
    assert r.status_code == 302
    h = {"Authorization": f"Bearer {_token(client)}"}
    fmt = next(f for f in client.get("/api/v1/import-formats", headers=h).json() if f["name"] == "WebTransforms")
    assert fmt["transforms"][0]["target"] == "sync:jira.issue_key"
    assert fmt["target_rules"][0]["set_target"] == "jira"


def test_web_format_save_drops_invalid_rules(client):
    _login_session(client)
    import json
    client.post("/import-formats", data={
        "name": "BadRules", "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": "{}",
        "transforms_json": json.dumps([{"target": "not_a_target", "op": "copy", "source": "X"}]),
        "target_rules_json": json.dumps([{"when": "sync:jira.issue_key", "set_target": "bogus"}]),
    }, follow_redirects=False)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fmt = next(f for f in client.get("/api/v1/import-formats", headers=h).json() if f["name"] == "BadRules")
    assert fmt["transforms"] == [] and fmt["target_rules"] == []


# ---------- Clockify workflow: one project, customer per tag ----------

def test_clockify_tag_routes_to_project_and_sets_export_target(client):
    """Workflow: alle Stunden in EINEM Clockify-Projekt, Kunde je Tag. Beim Import
    routet der Tag (= Projektcode ODER -name) in das passende TimeHub-Projekt, und
    das Exportziel kommt automatisch aus dem Projekt-Default — kein Eintrag-Override
    nötig. Kein Doppel-Projekt wird angelegt."""
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    # Zwei Kundenprojekte mit unterschiedlichem Exportziel (eindeutige Codes/Namen,
    # da die Test-DB session-weit geteilt ist).
    pa = client.post("/api/v1/projects", json={
        "name": "Clockify Acme", "code": "CLKACME", "default_sync_target": "salesforce",
        "sync_metadata": {"salesforce": {"assignment_id": "a01000000000001"}},
    }, headers=h).json()
    pb = client.post("/api/v1/projects", json={
        "name": "Clockify Beta", "code": "CLKB999", "default_sync_target": "jira",
    }, headers=h).json()
    assert "id" in pa and "id" in pb, (pa, pb)

    # Importformat: der Clockify-Export hat den Kunden in der Tags-Spalte. Tags
    # füttern sowohl die Projekt-Zuordnung (project_code) als auch das tags-Feld.
    fmt_id = client.post("/api/v1/import-formats", json={
        "name": "Clockify Tags", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "duration_hours": "Hours",
                       "project_code": "Tags", "tags": "Tags"},
    }, headers=h).json()["id"]
    csv = ("Date,Hours,Tags\n"
           "2027-03-15,1.5,CLKACME\n"           # matcht Projekt A per Code
           "2027-03-16,2,Clockify Beta\n")      # matcht Projekt B per Name (Fallback)
    r = client.post(f"/api/v1/import-formats/{fmt_id}/run",
                    files={"file": ("clockify.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["created_projects"] == []  # nichts neu angelegt, beide gematcht

    # Nur die Einträge dieses Tests (die DB ist session-weit geteilt).
    entries = client.get("/api/v1/time-entries", headers=h).json()
    mine = {e["project_id"]: e for e in entries
            if e["project_id"] in (pa["id"], pb["id"])}
    assert mine[pa["id"]]["entry_date"] == "2027-03-15"
    assert mine[pa["id"]]["tags"] == ["CLKACME"]
    assert mine[pb["id"]]["entry_date"] == "2027-03-16"

    # Exportziel kommt aus dem Projekt-Default → materialisierte EntrySync-Zeile.
    from app.db import SessionLocal
    from app.models import EntrySync
    with SessionLocal() as db:
        def targets(entry_id):
            return {es.target for es in db.query(EntrySync).filter(
                EntrySync.entry_id == entry_id).all()}
        assert targets(mine[pa["id"]]["id"]) == {"salesforce"}
        assert targets(mine[pb["id"]]["id"]) == {"jira"}


def test_clockify_multi_tag_cell_split_to_project(client):
    """Mehrere Tags pro Zelle (Clockify komma-separiert): ein split-Transform
    greift den Kunden-Tag heraus und routet ihn ins Projekt."""
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    client.post("/api/v1/projects", json={
        "name": "Clockify Gamma", "code": "CLKGAMMA", "default_sync_target": "bcs",
    }, headers=h)
    fmt_id = client.post("/api/v1/import-formats", json={
        "name": "Clockify Split", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "duration_hours": "Hours"},
        "transforms": [{"target": "project_code", "op": "split",
                        "source": "Tags", "sep": "|", "index": 0}],
    }, headers=h).json()["id"]
    # Tags-Zelle (eigener Separator |, damit das CSV-Komma nicht stört).
    csv = "Date,Hours,Tags\n2027-03-17,1,CLKGAMMA|billable\n"
    r = client.post(f"/api/v1/import-formats/{fmt_id}/run",
                    files={"file": ("c.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201 and r.json()["created"] == 1, r.text
    assert r.json()["created_projects"] == []
    pgamma = next(p for p in client.get("/api/v1/projects", headers=h).json()
                  if p["code"] == "CLKGAMMA")
    entries = client.get("/api/v1/time-entries", headers=h).json()
    e = next(e for e in entries if e["project_id"] == pgamma["id"])
    assert e["entry_date"] == "2027-03-17"


# ---------- export round-trip for sync fields ----------

def test_export_emits_sync_field_value():
    from app.services.reports import export_via_import_format
    entry = SimpleNamespace(
        entry_date=__import__("datetime").date(2026, 5, 27),
        start_time=None, end_time=None, duration_minutes=60, description="x",
        tags=[], sync_target_override="jira", external_ref=None,
        sync_metadata_override={"jira": {"issue_key": "ABC-1"}},
    )
    project = SimpleNamespace(code="ACME", default_sync_target="jira")
    user = SimpleNamespace(email="a@x", full_name="A")
    body, _enc = export_via_import_format(
        [(entry, project, user)], {"sync:jira.issue_key": "Ticket"}, separator=",",
    )
    lines = body.strip().splitlines()
    assert lines[0] == "Ticket" and lines[1] == "ABC-1"
