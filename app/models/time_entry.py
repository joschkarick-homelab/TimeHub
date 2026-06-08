from datetime import date, datetime, time

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, Time, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._enums import EntrySource, SyncStatus


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # Authoritative duration. UI enforces 15-min steps; storage stays exact.
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # When set, overrides the project's default_sync_target for this entry.
    sync_target_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When set (non-empty), overrides the resolved target set for this entry,
    # bypassing project defaults and rules. Empty/None = inherit.
    sync_targets_override: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Per-entry overrides for sync metadata (e.g. specific Jira issue key).
    sync_metadata_override: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default=SyncStatus.PENDING)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=EntrySource.MANUAL)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="time_entries")  # noqa: F821
    project: Mapped["Project"] = relationship(back_populates="time_entries")  # noqa: F821
    entry_syncs: Mapped[list["EntrySync"]] = relationship(  # noqa: F821
        back_populates="entry", cascade="all, delete-orphan", passive_deletes=True
    )
