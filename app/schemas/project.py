from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models._enums import ProjectStatus, SyncTarget


class ProjectBase(BaseModel):
    name: str
    code: str = Field(min_length=1, max_length=64)
    customer: str | None = None
    color: str = "#6366f1"
    status: ProjectStatus = ProjectStatus.ACTIVE
    default_sync_target: SyncTarget = SyncTarget.INTERN
    sync_metadata: dict = Field(default_factory=dict)


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    customer: str | None = None
    color: str | None = None
    status: ProjectStatus | None = None
    default_sync_target: SyncTarget | None = None
    sync_metadata: dict | None = None


class ProjectOut(ProjectBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
