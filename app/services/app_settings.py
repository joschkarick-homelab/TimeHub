"""Thin helpers over the AppSetting key/value table for global settings."""

from sqlalchemy.orm import Session

from app.models import AppSetting

AI_HINTS_KEY = "ai_mapping_hints"


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
    else:
        row.value = value
    db.add(row)
    db.commit()
