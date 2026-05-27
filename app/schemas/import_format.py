from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_TARGETS = {
    "entry_date",
    "start_time",
    "end_time",
    "duration_minutes",
    "duration_hours",
    "project_code",
    "description",
    "tags",
    "sync_target",
    "external_ref",
}


class ImportFormatBase(BaseModel):
    name: str
    source_hint: str = "custom"
    separator: str = ","
    encoding: str = "utf-8"
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M"
    column_map: dict[str, str] = Field(default_factory=dict)
    default_project_code: str | None = None
    notes: str = ""


class ImportFormatCreate(ImportFormatBase):
    is_global: bool = False  # ignored unless caller is admin


class ImportFormatUpdate(BaseModel):
    name: str | None = None
    source_hint: str | None = None
    separator: str | None = None
    encoding: str | None = None
    date_format: str | None = None
    time_format: str | None = None
    column_map: dict[str, str] | None = None
    default_project_code: str | None = None
    notes: str | None = None
    is_global: bool | None = None  # admin only


class ImportFormatOut(ImportFormatBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_global: bool
    owner_id: int | None
    created_at: datetime
    updated_at: datetime


class ImportFormatSuggestion(BaseModel):
    """AI-generated suggestion, not yet persisted."""

    source_hint: str
    separator: str
    encoding: str = "utf-8"
    date_format: str
    time_format: str
    column_map: dict[str, str]
    default_project_code: str | None = None
    notes: str = ""
    detected_headers: list[str] = Field(default_factory=list)
