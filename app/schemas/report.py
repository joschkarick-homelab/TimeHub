from datetime import date

from pydantic import BaseModel


class ReportFilters(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    user_id: int | None = None
    project_id: int | None = None
    sync_target: str | None = None
    tag: str | None = None
