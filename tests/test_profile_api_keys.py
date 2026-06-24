"""API-key management on the profile page: create (revealed once), list,
revoke, and that a freshly created key actually authenticates the JSON API."""

import re


def _login_session(client) -> None:
    r = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "testpass"},
        follow_redirects=False,
    )
    assert r.status_code == 302


def _extract_key(html: str) -> str | None:
    m = re.search(r"(thk_[A-Za-z0-9_\-]+)", html)
    return m.group(1) if m else None


def test_create_key_revealed_once_then_hidden(client):
    _login_session(client)

    created = client.post(
        "/profile/api-keys", data={"name": "Raycast"}, follow_redirects=False
    )
    assert created.status_code == 302

    # First render after creation reveals the full key and its label.
    page = client.get("/profile").text
    assert "Raycast" in page
    full = _extract_key(page)
    assert full and full.startswith("thk_")

    # A subsequent render must NOT show the full key again (only the prefix).
    page2 = client.get("/profile").text
    assert full not in page2
    assert "Raycast" in page2  # still listed by name/prefix

    # And the revealed key works for API auth.
    me = client.get("/api/v1/auth/me", headers={"X-API-Key": full})
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"


def test_revoke_key_removes_it_and_disables_auth(client):
    _login_session(client)
    client.post("/profile/api-keys", data={"name": "ToRevoke"}, follow_redirects=False)
    full = _extract_key(client.get("/profile").text)
    assert full

    # Find the key id from the list API (owned by the session user).
    keys = client.get("/api/v1/auth/api-keys", headers={"X-API-Key": full}).json()
    key_id = next(k["id"] for k in keys if k["name"] == "ToRevoke")

    revoked = client.post(
        f"/profile/api-keys/{key_id}/revoke", follow_redirects=False
    )
    assert revoked.status_code == 302

    # No longer listed as active on the profile page…
    assert "ToRevoke" not in client.get("/profile").text
    # …and the key can no longer authenticate.
    assert client.get("/api/v1/auth/me", headers={"X-API-Key": full}).status_code == 401


def test_revoke_other_users_key_is_noop_redirect(client):
    _login_session(client)
    # A non-existent / non-owned id just redirects with an error, never 500.
    r = client.post("/profile/api-keys/999999/revoke", follow_redirects=False)
    assert r.status_code == 302
    assert "error=" in r.headers["location"]


def test_create_key_without_name_defaults_label(client):
    _login_session(client)
    client.post("/profile/api-keys", data={"name": ""}, follow_redirects=False)
    page = client.get("/profile").text
    assert _extract_key(page)
    assert "API Key" in page
