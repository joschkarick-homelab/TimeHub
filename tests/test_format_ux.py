"""Phase 2.5 UX: duration op, live preview applying transforms, sample-data
persistence (so all columns stay available on edit), and AI refine on edit."""


def _login_session(client) -> None:
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


def _token(client) -> str:
    return client.post("/api/v1/auth/login",
                       json={"email": "admin@example.com", "password": "testpass"}).json()["access_token"]


# ---------- duration op (pure) ----------

def test_duration_op_to_minutes_and_hours():
    from app.services.transforms import apply_transform
    m = {"target": "duration_minutes", "op": "duration", "source": "Dur"}
    h = {"target": "duration_hours", "op": "duration", "source": "Dur"}
    assert apply_transform(m, {"Dur": "01:30:00"}) == "90"
    assert apply_transform(h, {"Dur": "01:30:00"}) == "1.5"
    assert apply_transform(m, {"Dur": "00:45"}) == "45"
    assert apply_transform(m, {"Dur": "kaputt"}) is None


# ---------- preview applies transforms ----------

def test_preview_reflects_duration_transform():
    from app.services.reports import preview_via_import_format
    sample = "Date,Dur\n2026-05-27,01:30:00\n"
    transforms = [{"target": "duration_minutes", "op": "duration", "source": "Dur"}]
    _src, target_rows = preview_via_import_format(
        sample, {"Date": "entry_date"}, separator=",", transforms=transforms,
    )
    assert target_rows[0]["duration_minutes"] == "90 min"


# ---------- full import with duration op ----------

def test_import_duration_op_end_to_end(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "DurFmt", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"Date": "entry_date", "Project": "project_code"},
        "transforms": [{"target": "duration_minutes", "op": "duration", "source": "Dur"}],
    }, headers=h).json()["id"]
    csv = "Date,Project,Dur\n2026-05-27,DURPROJ,01:30:00\n"
    r = client.post(f"/api/v1/import-formats/{fid}/run",
                    files={"file": ("d.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201 and r.json()["created"] == 1
    entries = client.get("/api/v1/time-entries", headers=h).json()
    rec = next(e for e in entries if e["entry_date"] == "2026-05-27"
               and e["duration_minutes"] == 90 and e.get("description") == "")
    assert rec["duration_minutes"] == 90


# ---------- sample persistence keeps all columns on edit ----------

def test_edit_keeps_ignored_columns_via_sample(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "WithSample", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"Date": "entry_date"},  # Dauer + Ignorierte nicht gemappt
        "sample_data": "Date,Dauer,Notiz\n2026-05-27,01:30:00,egal\n",
    }, headers=h).json()["id"]
    page = client.get(f"/import-formats/{fid}/edit")
    assert page.status_code == 200
    # the unmapped columns are still offered as mapping rows / transform sources
    assert "Dauer" in page.text and "Notiz" in page.text


def test_web_save_persists_sample(client):
    _login_session(client)
    import json
    client.post("/import-formats", data={
        "name": "SampleSave", "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": json.dumps({"Date": "entry_date"}),
        "transforms_json": "[]", "target_rules_json": "[]",
        "sample_text": "Date,Dur\n2026-05-27,01:30:00\n",
    }, follow_redirects=False)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fmt = next(f for f in client.get("/api/v1/import-formats", headers=h).json() if f["name"] == "SampleSave")
    assert "01:30:00" in (fmt["sample_data"] or "")


# ---------- live preview endpoint ----------

def test_preview_endpoint_renders_transform(client):
    _login_session(client)
    import json
    r = client.post("/import-formats/preview", data={
        "sample_text": "Date,Dur\n2026-05-27,01:30:00\n",
        "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": json.dumps({"Date": "entry_date"}),
        "transforms_json": json.dumps([{"target": "duration_minutes", "op": "duration", "source": "Dur"}]),
    })
    assert r.status_code == 200
    assert "90 min" in r.text


def test_preview_endpoint_requires_login(client):
    r = client.post("/import-formats/preview", data={"sample_text": "a\n1\n"})
    assert r.status_code == 401


# ---------- AI refine on the edit screen (stubbed) ----------

def test_edit_refine_stays_on_edit_and_applies_ai(client, monkeypatch):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "RefineEdit", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"Date": "entry_date"},
        "sample_data": "Date,Dur\n2026-05-27,01:30:00\n",
    }, headers=h).json()["id"]

    import app.web.router as router
    from app.schemas.import_format import ImportFormatSuggestion

    def fake_suggest(text, *, instruction=None, previous=None):
        return ImportFormatSuggestion(
            source_hint="custom", separator=",", encoding="utf-8",
            date_format="%Y-%m-%d", time_format="%H:%M",
            column_map={"Date": "entry_date"},
            transforms=[{"target": "duration_minutes", "op": "duration", "source": "Dur"}],
            target_rules=[], notes="Dauer umgerechnet.", detected_headers=["Date", "Dur"],
        )

    monkeypatch.setattr(router, "suggest_mapping", fake_suggest)
    r = client.post(f"/import-formats/{fid}/refine", data={
        "name": "RefineEdit", "sample_text": "Date,Dur\n2026-05-27,01:30:00\n",
        "instruction": "Dauer aus Dur in Minuten umrechnen",
        "separator": ",", "date_format": "%Y-%m-%d", "time_format": "%H:%M",
        "column_map_json": '{"Date": "entry_date"}', "transforms_json": "[]", "target_rules_json": "[]",
    })
    assert r.status_code == 200
    # stayed on the edit screen for this format
    assert f"/import-formats/{fid}/edit" in r.text
    # the AI's duration transform is now embedded in the editor
    assert "duration_minutes" in r.text
    # and the live preview already reflects it
    assert "90 min" in r.text
