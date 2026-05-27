from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CsvColumn(BaseModel):
    header: str
    field: str  # one of: date, start_time, end_time, duration_hours, duration_minutes,
    # project_code, project_name, customer, user_email, description, tags,
    # sync_target, external_ref
    format: str | None = None


class CsvTemplateBase(BaseModel):
    name: str
    columns: list[CsvColumn]
    separator: str = ";"
    date_format: str = "%Y-%m-%d"
    encoding: str = "utf-8"
    decimal_separator: str = ","


class CsvTemplateCreate(CsvTemplateBase):
    pass


class CsvTemplateUpdate(BaseModel):
    name: str | None = None
    columns: list[CsvColumn] | None = None
    separator: str | None = None
    date_format: str | None = None
    encoding: str | None = None
    decimal_separator: str | None = None


class CsvTemplateOut(CsvTemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class CsvImportMapping(BaseModel):
    """Maps source CSV columns to TimeHub fields for /intake/csv."""

    separator: str = ";"
    encoding: str = "utf-8"
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M"
    # source column header -> target field name
    column_map: dict[str, str] = Field(
        default_factory=dict,
        description="Map of source CSV column header to TimeHub field. Supported targets: "
        "entry_date, start_time, end_time, duration_minutes, duration_hours, "
        "project_code, description, tags, sync_target, external_ref",
    )
    default_project_code: str | None = None
