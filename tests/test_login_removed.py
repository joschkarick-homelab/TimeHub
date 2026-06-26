def test_password_login_routes_gone(raw_client):
    assert raw_client.get("/login").status_code == 404
    assert raw_client.post("/auth/login", json={"email": "a", "password": "b"}).status_code == 404


def test_m365_sso_login_route_gone(raw_client):
    assert raw_client.get("/auth/m365/login", follow_redirects=False).status_code == 404
