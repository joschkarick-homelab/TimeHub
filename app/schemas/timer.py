from datetime import datetime

from pydantic import BaseModel, Field


class TimerStart(BaseModel):
    """Start a running timer. Everything is optional: a timer can be started
    bare and have its project/description filled in later (PATCH /timer/current)
    or at stop time. Identify the project by id or code; ``started_at`` may
    backdate the start and defaults to now on the server."""

    project_id: int | None = None
    project_code: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    started_at: datetime | None = None


class TimerUpdate(BaseModel):
    """Patch the running timer. Only provided fields change; pass an empty
    ``project_code``/``project_id`` is not how you clear it (omit to keep)."""

    project_id: int | None = None
    project_code: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class TimerStop(BaseModel):
    """Stop the running timer and materialize a TimeEntry. Optional overrides
    let the client tweak the entry at stop time, including assigning a project
    if the timer was started without one. ``round_to_minutes`` rounds the
    computed duration up to the nearest multiple (e.g. 15); omit for exact
    minutes."""

    project_id: int | None = None
    project_code: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    round_to_minutes: int | None = Field(default=None, ge=1, le=240)


class TimerOut(BaseModel):
    """The running timer. Clients tick locally from ``started_at`` rather than
    polling; ``elapsed_seconds`` is a convenience snapshot at response time.
    Project fields are null when no project is assigned yet."""

    id: int
    project_id: int | None
    project_code: str | None
    project_name: str | None
    description: str
    tags: list[str]
    started_at: datetime
    elapsed_seconds: int

