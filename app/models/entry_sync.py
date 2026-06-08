from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._enums import SyncStatus


class EntrySync(Base):
    """Per-target sync state for a single time entry.

    One row per (entry × target). Replaces the single `TimeEntry.sync_status`
    once an entry may go to several targets at once: each target tracks its own
    status, remote reference and last error independently.
    """

    __tablename__ = "entry_syncs"
    __table_args__ = (UniqueConstraint("entry_id", "target", name="uq_entry_syncs_entry_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("time_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SyncStatus.PENDING)
    # Remote id in the target system (e.g. Zeiterfassung__c id / Jira worklog id).
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    entry: Mapped["TimeEntry"] = relationship(back_populates="entry_syncs")  # noqa: F821
