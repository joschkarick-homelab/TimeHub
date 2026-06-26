from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class M365Connection(Base):
    """A user's connected Microsoft 365 mailbox for the read-only calendar
    overlay. One row per user (the calendar view shows the signed-in user's own
    calendar). OAuth tokens are stored Fernet-encrypted, like the SF secrets;
    ``token_expires_at`` drives on-demand refresh in app.services.m365."""

    __tablename__ = "m365_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    # Display-only: the connected UPN/mail, shown on the profile page.
    account: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    access_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Last fetch/refresh error, surfaced on the profile page so a user whose
    # consent lapsed knows to reconnect.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="m365_connection")  # noqa: F821
