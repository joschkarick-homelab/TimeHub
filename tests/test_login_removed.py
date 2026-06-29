def test_password_login_routes_gone(raw_client):
    assert raw_client.get("/login").status_code == 404
    assert raw_client.post("/auth/login", json={"email": "a", "password": "b"}).status_code == 404


def test_m365_sso_login_route_gone(raw_client):
    assert raw_client.get("/auth/m365/login", follow_redirects=False).status_code == 404


def test_create_user_without_password(client):
    """The manual-create password is inert under Hub auth: an admin can create a
    user via POST /api/v1/users with no password, and the row stores no hash."""
    r = client.post(
        "/api/v1/users",
        json={"email": "nopw@example.com", "full_name": "No PW"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "nopw@example.com"

    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.query(User).filter_by(email="nopw@example.com").one()
        assert user.hashed_password is None
