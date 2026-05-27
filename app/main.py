import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.api import api_router
from app.config import get_settings
from app.services.bootstrap import ensure_initial_admin
from app.web.router import router as web_router

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")

app.include_router(api_router)
app.include_router(web_router)


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
