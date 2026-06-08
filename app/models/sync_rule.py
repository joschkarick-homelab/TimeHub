from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SyncRule(Base):
    """Declarative rule that refines an entry's target set on top of the
    project's default `sync_targets`.

    Evaluated at entry creation/import time (see services.sync_rules). The
    condition vocabulary is kept deliberately small and extensible, mirroring
    the field registry in services.sync_fields.
    """

    __tablename__ = "sync_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Lower runs first; global rules before project-scoped on equal priority.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="global")  # global|project
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Condition, e.g. {"type": "has_tag", "values": ["billable"]}.
    condition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # add_target|remove_target|set_targets
    target: Mapped[str | None] = mapped_column(String(32), nullable=True)  # for add/remove
    targets: Mapped[list | None] = mapped_column(JSON, nullable=True)  # for set_targets

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
