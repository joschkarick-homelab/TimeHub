"""Pure-ASGI middleware enforcing API-key write scope.

For unsafe HTTP methods under /api/v1, a request authenticated with an API key
whose scope doesn't permit writing to that path is rejected with 403 before it
reaches the route. Bearer/session requests carry no X-API-Key and pass through;
invalid/expired keys also pass through here and are rejected (401) by the auth
dependency. Pure ASGI (not BaseHTTPMiddleware) so it never buffers the MCP
stream mounted at /mcp.
"""

import json

from app.db import SessionLocal
from app.deps import api_key_scope, scope_allows_write

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _send_403(send, scope_name: str, path: str) -> None:
    body = json.dumps(
        {"detail": f"API key scope '{scope_name}' is read-only for {path}."}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ApiKeyWriteScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope.get("method") in _UNSAFE_METHODS
            and scope.get("path", "").startswith("/api/v1/")
        ):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            raw_key = headers.get(b"x-api-key")
            if raw_key:
                db = SessionLocal()
                try:
                    key_scope = api_key_scope(raw_key.decode("latin-1").strip(), db)
                finally:
                    db.close()
                # Unknown/expired keys (None) fall through to the auth 401.
                if key_scope is not None and not scope_allows_write(key_scope, scope["path"]):
                    await _send_403(send, key_scope, scope["path"])
                    return
        await self.app(scope, receive, send)
