import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.api import api_router
from app.config import get_settings
from app.scope_mw import ApiKeyWriteScopeMiddleware
from app.services.bootstrap import ensure_builtin_formats
from app.web.router import LoginRequired
from app.web.router import router as web_router

_STATIC_DIR = Path(__file__).parent / "static"

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_builtin_formats()
    if settings.mcp_enabled:
        # Run the MCP session manager alongside the app for the /mcp endpoint.
        from app import mcp_server

        async with mcp_server.session_lifespan():
            yield
    else:
        yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Zentrale Zeiterfassung – API für Erfassung, Import, Export und Reporting.",
    lifespan=lifespan,
    root_path=settings.normalized_base_path,
)

# A wildcard origin with credentials is unsafe (and spec-invalid). The API
# authenticates via Bearer/API-key, never via the session cookie cross-origin,
# so only enable credentialed CORS once an explicit allowlist is configured.
_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)
# Reject writes from read-only / tracking-scoped API keys before they hit a route.
app.add_middleware(ApiKeyWriteScopeMiddleware)


class HtmlNoCacheASGI:
    """Append Cache-Control: no-cache to text/html responses. Pure ASGI: it
    inspects only the http.response.start message and passes body frames
    through untouched (safe for streaming/SSE)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if headers.get("content-type", "").startswith("text/html"):
                    headers["Cache-Control"] = "no-cache, must-revalidate"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(HtmlNoCacheASGI)

@app.exception_handler(LoginRequired)
async def _login_required_handler(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "Not authenticated"}, status_code=401)


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(api_router)
app.include_router(web_router)

if settings.mcp_enabled:
    # Remote MCP server (Streamable HTTP) at /mcp; authenticates via Hub X-MSQ
    # identity (HubIdentityAuthMiddleware) / mcp-bearer, not an API key.
    from app import mcp_server

    app.mount("/mcp", mcp_server.build_asgi_app())


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/healthz", tags=["system"])
def healthz() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": __version__}


@app.get("/readyz", tags=["system"])
def readyz() -> dict:
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ready"}
