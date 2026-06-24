from datetime import date

from pydantic import BaseModel


class WeeklyProjectHours(BaseModel):
    project_id: int
    code: str
    name: str
    minutes: int
    hours: float


class WeeklyTargetHours(BaseModel):
    target: str
    minutes: int
    hours: float


class WeeklyHours(BaseModel):
    date_from: date
    date_to: date
    total_minutes: int
    total_hours: float
    entry_count: int
    by_project: list[WeeklyProjectHours]
    by_target: list[WeeklyTargetHours]


class ReportFilters(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    user_id: int | None = None
    project_id: int | None = None
    sync_target: str | None = None
    tag: str | None = None
