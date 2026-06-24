from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ActiveTimer(Base):
    """A running stopwatch for a user. TimeHub itself stores only post-hoc
    entries, so the timer keeps just the start timestamp and the metadata the
    eventual entry will need; clients (Raycast menu bar, MCP) compute elapsed
    time locally from ``started_at``. Stopping the timer materializes a
    TimeEntry and removes this row. At most one timer per user."""

    __tablename__ = "active_timers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One running timer per user — enforced by the unique FK.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    # Optional: a timer can be started without a project and have one assigned
    # later (e.g. via a dropdown) before it is stopped.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )

    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # When tracking began. Authoritative for the elapsed/duration calculation.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()  # noqa: F821
    project: Mapped["Project"] = relationship()  # noqa: F821
