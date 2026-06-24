from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._enums import ProjectStatus, SyncTarget


class Project(Base):
    __tablename__ = "projects"
    # Projects are per-user: the same code may exist once per owner.
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_projects_user_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    customer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#6366f1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ProjectStatus.ACTIVE)

    default_sync_target: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SyncTarget.INTERN
    )
    # Default target set for this project's entries (multi-target). When empty,
    # callers fall back to the single `default_sync_target` above for back-compat.
    sync_targets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Per-target metadata, e.g. {"jira": {"project_key": "ABC", "default_issue": "ABC-1"},
    #                            "salesforce": {"project_id": "..."},
    #                            "bcs": {"project_no": "..."}}
    sync_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    time_entries: Mapped[list["TimeEntry"]] = relationship(back_populates="project")  # noqa: F821

    @property
    def display_label(self) -> str:
        """Human label for dropdowns: "Name (Kunde)". The internal project code
        is deliberately omitted (it confuses users without adding value); it
        falls back to the code only when no name is set (e.g. auto-created
        projects), and omits the customer suffix when none is set."""
        name = (self.name or "").strip()
        code = (self.code or "").strip()
        customer = (self.customer or "").strip()
        base = name or code
        return f"{base} ({customer})" if customer else base
