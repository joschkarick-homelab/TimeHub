"""Web UI router: aggregates the per-domain route modules under one
APIRouter that carries the shared CSRF dependency. Shared helpers live in
app.web.common; LoginRequired is re-exported for the exception handler in
app.main."""

from fastapi import APIRouter, Depends

from app.web.common import LoginRequired, csrf_protect
from app.web.routes import (
    account,
    admin,
    calendar,
    dashboard,
    entries,
    formats,
    m365,
    m365_login,
    projects,
    reports,
    sync,
    views,
)

router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])

for _m in (
    account,
    admin,
    calendar,
    dashboard,
    entries,
    formats,
    m365,
    m365_login,
    projects,
    reports,
    sync,
    views,
):
    router.include_router(_m.router)

__all__ = ["router", "LoginRequired"]
