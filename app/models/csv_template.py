from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CsvTemplate(Base):
    """Reusable CSV export profile (column mapping, separator, date format, encoding)."""

    __tablename__ = "csv_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    # Ordered list of {"header": "...", "field": "date|project|...", "format": "..."}
    columns: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    separator: Mapped[str] = mapped_column(String(4), nullable=False, default=";")
    date_format: Mapped[str] = mapped_column(String(32), nullable=False, default="%Y-%m-%d")
    encoding: Mapped[str] = mapped_column(String(16), nullable=False, default="utf-8")
    decimal_separator: Mapped[str] = mapped_column(String(2), nullable=False, default=",")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
