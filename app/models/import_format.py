from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ImportFormat(Base):
    """Reusable CSV import profile (Toggl / Clockify / custom).

    Owned by a user. If is_global=True the format is visible to everyone;
    only admins can flip that flag.
    """

    __tablename__ = "import_formats"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_hint: Mapped[str] = mapped_column(String(64), nullable=False, default="custom")

    separator: Mapped[str] = mapped_column(String(4), nullable=False, default=",")
    encoding: Mapped[str] = mapped_column(String(16), nullable=False, default="utf-8")
    date_format: Mapped[str] = mapped_column(String(32), nullable=False, default="%Y-%m-%d")
    time_format: Mapped[str] = mapped_column(String(32), nullable=False, default="%H:%M")

    # source CSV header -> target TimeHub field name
    column_map: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    default_project_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_global: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    notes: Mapped[str] = mapped_column(String(1024), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    owner: Mapped["User | None"] = relationship()  # noqa: F821
