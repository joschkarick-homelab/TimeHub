from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SavedView(Base):
    """A user-saved filter/grouping configuration, recallable on the dashboard
    or the reports page.

    Lets users pin standing views like "Monatsreport Kunde X" instead of
    rebuilding the same filters every time. The ``date_range`` token keeps a
    saved view meaningful over time (e.g. a "this_month" report always tracks
    the current month); only ``custom`` falls back to the explicit dates.
    Grouping (``group_by``/``detailed``) is reports-only and ignored for
    dashboard views.
    """

    __tablename__ = "saved_views"
    # Names are unique per user *and* kind, so a dashboard and a report view may
    # share a name without colliding.
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "name", name="uq_saved_views_user_kind_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 'dashboard' | 'reports' — which page the view belongs to.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="reports")
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Relative range token ('all', 'this_week', 'last_week', 'this_month',
    # 'last_month', 'this_year', 'custom'). 'custom' uses date_from/date_to.
    date_range: Mapped[str] = mapped_column(String(24), nullable=False, default="custom")
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Plain nullable FK: if the project is removed the view degrades to "all
    # projects" rather than breaking.
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    customer: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Reports-only grouping config.
    group_by: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    detailed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
