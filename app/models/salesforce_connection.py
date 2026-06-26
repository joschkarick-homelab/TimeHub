from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SalesforceConnection(Base):
    """A user's per-user Salesforce OAuth link.

    TimeHub authenticates to Salesforce on this user's behalf via the OAuth web
    flow (the org's login delegates to Microsoft 365 SSO), so the sync runs with
    the user's own Salesforce permissions instead of the shared service account.
    One row per user. Tokens are Fernet-encrypted at rest, like the M365
    tokens; ``token_expires_at`` drives on-demand refresh in
    ``app.services.salesforce`` (a 401 also triggers a refresh as a backstop).
    """

    __tablename__ = "salesforce_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    # Display-only: the connected Salesforce username/identity, shown on profile.
    account: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # The per-connection Salesforce instance host the tokens are valid against.
    instance_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    access_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Last refresh/query error, surfaced on the profile page so a user whose
    # token was revoked knows to reconnect.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="salesforce_connection")  # noqa: F821
