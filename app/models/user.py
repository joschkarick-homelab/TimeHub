from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Nullable: accounts provisioned via Microsoft 365 SSO have no local
    # password. Password-login routes treat a missing hash as "no password set"
    # and reject the password flow for that user (SSO-only).
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stable Entra object id (the ID token's ``oid`` claim) for SSO users, so the
    # link survives a mailbox/UPN rename. NULL for password-only accounts.
    entra_oid: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    # Stable Hub subject id from X-MSQ-User-Id. Opaque — do NOT assume it equals
    # the Entra oid (contract A.1.4). Primary match key behind the Agent Hub.
    msq_user_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # External system mappings for future sync targets. Salesforce PSA needs
    # a Contact ID (Resource = Contact, not User in the PSA data model);
    # the User ID is captured too so admins can resolve Contact via the
    # User->Contact link in SF if they prefer.
    salesforce_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    salesforce_contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Personal standing instructions for the AI import-format assistant.
    ai_hints: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    time_entries: Mapped[list["TimeEntry"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )
    # Optional read-only Microsoft 365 calendar link (one mailbox per user).
    m365_connection: Mapped["M365Connection | None"] = relationship(  # noqa: F821
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    # Optional per-user Salesforce OAuth link (the org logs in via M365 SSO).
    salesforce_connection: Mapped["SalesforceConnection | None"] = relationship(  # noqa: F821
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
