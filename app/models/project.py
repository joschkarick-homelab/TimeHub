from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._enums import ProjectStatus, SyncTarget


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    customer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#6366f1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ProjectStatus.ACTIVE)

    default_sync_target: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SyncTarget.INTERN
    )
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
        """Human label for dropdowns. Avoids "X – X" when code and name are
        identical (typical for auto-created projects from CSV imports)."""
        name = (self.name or "").strip()
        code = (self.code or "").strip()
        if not name or name.casefold() == code.casefold():
            return code
        return f"{code} – {name}"
