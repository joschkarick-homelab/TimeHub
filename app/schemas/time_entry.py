from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models._enums import EntrySource, SyncStatus, SyncTarget


class TimeEntryBase(BaseModel):
    project_id: int
    entry_date: date
    start_time: time | None = None
    end_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    sync_target_override: SyncTarget | None = None
    sync_metadata_override: dict = Field(default_factory=dict)
    external_ref: str | None = None

    @model_validator(mode="after")
    def _derive_duration(self) -> "TimeEntryBase":
        if self.duration_minutes is None:
            if self.start_time is None or self.end_time is None:
                raise ValueError("Provide duration_minutes or start_time + end_time")
            start_total = self.start_time.hour * 60 + self.start_time.minute
            end_total = self.end_time.hour * 60 + self.end_time.minute
            delta = end_total - start_total
            if delta <= 0:
                raise ValueError("end_time must be after start_time")
            self.duration_minutes = delta
        return self


class TimeEntryCreate(TimeEntryBase):
    # Admin can post on behalf of another user; otherwise defaults to current user.
    user_id: int | None = None


class TimeEntryBulkCreate(BaseModel):
    entries: list[TimeEntryCreate]


class TimeEntryUpdate(BaseModel):
    project_id: int | None = None
    entry_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1)
    description: str | None = None
    tags: list[str] | None = None
    sync_target_override: SyncTarget | None = None
    sync_metadata_override: dict | None = None
    sync_status: SyncStatus | None = None
    external_ref: str | None = None


class TimeEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    project_id: int
    entry_date: date
    start_time: time | None
    end_time: time | None
    duration_minutes: int
    description: str
    tags: list[str]
    sync_target_override: SyncTarget | None
    sync_metadata_override: dict
    sync_status: SyncStatus
    source: EntrySource
    external_ref: str | None
    created_at: datetime
    updated_at: datetime


class BulkResult(BaseModel):
    created: int
    failed: int
    errors: list[dict] = Field(default_factory=list)
    ids: list[int] = Field(default_factory=list)
