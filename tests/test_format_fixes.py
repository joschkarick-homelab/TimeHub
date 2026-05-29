"""Follow-up fixes: clock durations parse natively on import, the import CSV is
remembered as the format's sample, and configurable AI standing instructions."""


def _login_session(client) -> None:
    r = client.post("/login", data={"email": "admin@example.com", "password": "testpass"},
                    follow_redirects=False)
    assert r.status_code == 302


def _token(client) -> str:
    return client.post("/api/v1/auth/login",
                       json={"email": "admin@example.com", "password": "testpass"}).json()["access_token"]


def _suggestion():
    from app.schemas.import_format import ImportFormatSuggestion
    return ImportFormatSuggestion(
        source_hint="custom", separator=",", encoding="utf-8",
        date_format="%Y-%m-%d", time_format="%H:%M",
        column_map={"description": "A"}, transforms=[], target_rules=[],
        default_project_code=None, notes="", detected_headers=["A", "B"],
    )


# ---------- issue 3: clock durations parse natively ----------

def test_parse_duration_field_clock_and_numeric():
    from app.services.csv_import import _parse_duration_field
    assert _parse_duration_field("01:30:00", as_hours=False) == 90
    assert _parse_duration_field("01:30:00", as_hours=True) == 90   # colon wins over unit
    assert _parse_duration_field("01:30", as_hours=False) == 90
    assert _parse_duration_field("1,5", as_hours=True) == 90        # decimal hours
    assert _parse_duration_field("30", as_hours=False) == 30        # plain minutes
    assert _parse_duration_field("kaputt", as_hours=False) is None


def test_auto_duration_detects_unit():
    from app.services.transforms import auto_duration_to_minutes
    assert auto_duration_to_minutes("01:30:00") == 90   # clock
    assert auto_duration_to_minutes("1,5") == 90        # decimal hours
    assert auto_duration_to_minutes("1.5") == 90
    assert auto_duration_to_minutes("90") == 90         # plain minutes
    assert auto_duration_to_minutes("") is None


def test_import_unified_duration_target(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "AutoDur", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "project_code": "Project", "duration": "Dur"},
    }, headers=h).json()["id"]
    # one row per format-supported unit, all should land as 90 minutes
    csv = ("Date,Project,Dur\n"
           "2026-06-01,AUTODUR,01:30:00\n"
           "2026-06-02,AUTODUR,1.5\n"
           "2026-06-03,AUTODUR,90\n")
    r = client.post(f"/api/v1/import-formats/{fid}/run",
                    files={"file": ("a.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201 and r.json()["created"] == 3, r.json()
    entries = client.get("/api/v1/time-entries", headers=h).json()
    mins = {e["entry_date"]: e["duration_minutes"] for e in entries
            if e["entry_date"] in ("2026-06-01", "2026-06-02", "2026-06-03")}
    assert mins == {"2026-06-01": 90, "2026-06-02": 90, "2026-06-03": 90}


def test_duration_target_label_and_options(client):
    _login_session(client)
    # the format wizard offers a single auto duration option, clearly labelled
    page = client.get("/import-formats/new")
    assert "Dauer (automatisch)" in page.text


# ---------- project code is optional / auto-generated ----------

def test_project_code_auto_from_name(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    client.post("/projects", data={"name": "Acme Website", "default_sync_target": "intern",
                                    "status": "active"}, follow_redirects=False)
    projects = client.get("/api/v1/projects", headers=h).json()
    p = next(p for p in projects if p["name"] == "Acme Website")
    assert p["code"] == "ACME-WEBSITE"
    # same name again → still unique
    client.post("/projects", data={"name": "Acme Website", "default_sync_target": "intern",
                                    "status": "active"}, follow_redirects=False)
    codes = [x["code"] for x in client.get("/api/v1/projects", headers=h).json()
             if x["name"] == "Acme Website"]
    assert len(set(codes)) == 2 and "ACME-WEBSITE-2" in codes


def test_explicit_project_code_still_honored(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    client.post("/projects", data={"name": "Custom", "code": "MYKEY",
                                    "default_sync_target": "intern", "status": "active"},
                follow_redirects=False)
    assert any(p["code"] == "MYKEY" for p in client.get("/api/v1/projects", headers=h).json())


# ---------- point 2: preview reads the right form ----------

def test_format_edit_has_named_form(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "FormId", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date"}, "sample_data": "Date\n2026-05-27\n",
    }, headers=h).json()["id"]
    page = client.get(f"/import-formats/{fid}/edit")
    assert 'id="format-form"' in page.text
    # the sample is an editable textarea, not hidden
    assert 'name="sample_text"' in page.text


def _run_clock(client, code, target_field):
    """Build a format mapping a HH:MM:SS column straight to a duration field
    (no transform) and import one row of 01:30:00."""
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": f"Clock {code}", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "project_code": "Project", target_field: "Dur"},
    }, headers=h).json()["id"]
    csv = f"Date,Project,Dur\n2026-05-27,{code},01:30:00\n"
    r = client.post(f"/api/v1/import-formats/{fid}/run",
                    files={"file": ("c.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 1, r.json()
    entries = client.get("/api/v1/time-entries", headers=h).json()
    return [e for e in entries if e["entry_date"] == "2026-05-27"
            and e["duration_minutes"] == 90]


def test_import_clock_duration_mapped_to_minutes(client):
    _login_session(client)
    assert _run_clock(client, "CLKMIN", "duration_minutes")


def test_import_clock_duration_mapped_to_hours(client):
    _login_session(client)
    # "01:30:00" is a clock value → 90 minutes regardless of the field unit
    assert _run_clock(client, "CLKHRS", "duration_hours")


# ---------- issue 2: import remembers the CSV as the format's sample ----------

def test_import_stores_sample_when_empty(client):
    _login_session(client)
    h = {"Authorization": f"Bearer {_token(client)}"}
    fid = client.post("/api/v1/import-formats", json={
        "name": "NoSampleYet", "separator": ",", "date_format": "%Y-%m-%d",
        "column_map": {"entry_date": "Date", "project_code": "Project"},
    }, headers=h).json()["id"]
    assert (client.get(f"/api/v1/import-formats/{fid}", headers=h).json().get("sample_data")) in (None, "")
    csv = "Date,Project,Ignored\n2026-05-27,SMPLP,whatever\n"
    client.post(f"/api/v1/import-formats/{fid}/run",
                files={"file": ("s.csv", csv, "text/csv")}, headers=h)
    fmt = client.get(f"/api/v1/import-formats/{fid}", headers=h).json()
    assert "Ignored" in (fmt["sample_data"] or "")
    # a second, different import must NOT overwrite the stored sample
    client.post(f"/api/v1/import-formats/{fid}/run",
                files={"file": ("s2.csv", "Date,Project,Ignored\n2026-05-28,SMPLP,other\n", "text/csv")},
                headers=h)
    assert "whatever" in (client.get(f"/api/v1/import-formats/{fid}", headers=h).json()["sample_data"])


# ---------- issue 1: configurable AI standing instructions ----------

def test_prompt_warns_about_clock_durations():
    from app.services.ai_mapping import _full_system_prompt
    p = _full_system_prompt()
    assert "01:30:00" in p and "90 minutes" in p


def test_global_ai_hints_persist_and_show(client):
    _login_session(client)
    r = client.post("/settings/ai-hints", data={"ai_hints": "Dauer ist HH:MM:SS"},
                    follow_redirects=False)
    assert r.status_code == 302
    page = client.get("/users")
    assert "Dauer ist HH:MM:SS" in page.text


def test_ai_hints_combine_global_and_user(client, monkeypatch):
    _login_session(client)
    import app.web.router as router
    captured = {}

    def capture(text, *, instruction=None, previous=None, hints=None):
        captured["hints"] = hints or ""
        return _suggestion()

    monkeypatch.setattr(router, "suggest_mapping", capture)
    client.post("/settings/ai-hints", data={"ai_hints": "GLOBAL-RULE"}, follow_redirects=False)
    client.post("/profile", data={"full_name": "Admin", "ai_hints": "USER-RULE"},
                follow_redirects=False)
    r = client.post("/import-formats/new", data={"name": "HintFmt"},
                    files={"sample": ("s.csv", "A,B\n1,2\n", "text/csv")})
    assert r.status_code == 200
    assert "GLOBAL-RULE" in captured["hints"] and "USER-RULE" in captured["hints"]
