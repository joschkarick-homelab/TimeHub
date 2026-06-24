from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class TimerStart(BaseModel):
    """Start a running timer. Identify the project by id or by code; at least
    one is required. ``started_at`` may backdate the start (e.g. "I began 10
    minutes ago"); it defaults to now on the server."""

    project_id: int | None = None
    project_code: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    started_at: datetime | None = None

    @model_validator(mode="after")
    def _need_project(self) -> "TimerStart":
        if self.project_id is None and not (self.project_code or "").strip():
            raise ValueError("Provide project_id or project_code")
        return self


class TimerStop(BaseModel):
    """Stop the running timer and materialize a TimeEntry. Optional overrides
    let the client tweak the entry at stop time. ``round_to_minutes`` rounds the
    computed duration up to the nearest multiple (e.g. 15) to match the UI's
    granularity; omit for exact minutes."""

    description: str | None = None
    tags: list[str] | None = None
    round_to_minutes: int | None = Field(default=None, ge=1, le=240)


class TimerOut(BaseModel):
    """The running timer. Clients tick locally from ``started_at`` rather than
    polling; ``elapsed_seconds`` is a convenience snapshot at response time."""

    id: int
    project_id: int
    project_code: str
    project_name: str
    description: str
    tags: list[str]
    started_at: datetime
    elapsed_seconds: int
