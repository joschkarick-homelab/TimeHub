"""First-run bootstrap: create initial admin from env if no users exist."""

import logging

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import User
from app.security import hash_password

log = logging.getLogger(__name__)


def ensure_initial_admin() -> None:
    settings = get_settings()
    if not (settings.initial_admin_email and settings.initial_admin_password):
        return
    with SessionLocal() as db:
        exists = db.execute(select(User.id).limit(1)).first()
        if exists:
            return
        admin = User(
            email=settings.initial_admin_email,
            full_name=settings.initial_admin_name,
            hashed_password=hash_password(settings.initial_admin_password),
            is_admin=True,
            is_active=True,
        )
        db.add(admin)
        db.commit()
        log.info("Bootstrapped initial admin user '%s'", admin.email)
