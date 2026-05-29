from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services import sync_fields as _sf

_BASE_TARGETS = {
    "entry_date",
    "start_time",
    "end_time",
    "duration",
    "duration_minutes",
    "duration_hours",
    "project_code",
    "description",
    "tags",
    "sync_target",
    "external_ref",
}
# Entry-level sync fields (e.g. sync:jira.issue_key) are valid mapping targets.
SUPPORTED_TARGETS = _BASE_TARGETS | _sf.entry_field_targets()


class ImportFormatBase(BaseModel):
    name: str
    source_hint: str = "custom"
    separator: str = ","
    encoding: str = "utf-8"
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M"
    column_map: dict[str, str] = Field(default_factory=dict)
    transforms: list[dict] = Field(default_factory=list)
    target_rules: list[dict] = Field(default_factory=list)
    sample_data: str | None = None
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
    transforms: list[dict] | None = None
    target_rules: list[dict] | None = None
    sample_data: str | None = None
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
    transforms: list[dict] = Field(default_factory=list)
    target_rules: list[dict] = Field(default_factory=list)
    default_project_code: str | None = None
    notes: str = ""
    detected_headers: list[str] = Field(default_factory=list)
