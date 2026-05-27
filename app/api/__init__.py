from fastapi import APIRouter

from app.api import auth, csv_templates, intake, projects, reports, time_entries, users

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(time_entries.router)
api_router.include_router(intake.router)
api_router.include_router(reports.router)
api_router.include_router(csv_templates.router)
