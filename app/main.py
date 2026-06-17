import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.api import api_router
from app.config import get_settings
from app.services.bootstrap import ensure_initial_admin
from app.web.router import LoginRequired
from app.web.router import router as web_router

_STATIC_DIR = Path(__file__).parent / "static"

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_initial_admin()
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Zentrale Zeiterfassung – API für Erfassung, Import, Export und Reporting.",
    lifespan=lifespan,
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

@app.exception_handler(LoginRequired)
async def _login_required_handler(request, exc) -> RedirectResponse:
    # Protected web pages raise LoginRequired when there's no session; send the
    # visitor to the login screen (same 302 the routes used to return inline).
    return RedirectResponse(url="/login", status_code=302)


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(_STATIC_DIR / "icon.svg", media_type="image/svg+xml")


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
