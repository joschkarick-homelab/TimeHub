from app.web.templating import join_base


def test_join_base_prefixes_root_path():
    assert join_base("/timehub", "/static/app.js") == "/timehub/static/app.js"
    assert join_base("", "/static/app.js") == "/static/app.js"
    assert join_base("/timehub", "static/app.js") == "/timehub/static/app.js"
    assert join_base("/timehub/", "/x") == "/timehub/x"


def test_health_endpoint_no_auth(raw_client):
    # /health must work with NO identity (raw_client has no X-MSQ headers).
    r = raw_client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_html_pages_carry_no_cache(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")
