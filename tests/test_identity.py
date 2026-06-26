from app.db import SessionLocal
from app.identity import HubPrincipal, resolve_user


def _principal(**kw):
    base = dict(subject="msq-1", email="new@mindsquare.de", name="New User",
                roles=frozenset(), guest=False)
    base.update(kw)
    return HubPrincipal(**base)


def test_resolve_provisions_unknown_user():
    with SessionLocal() as db:
        u = resolve_user(db, _principal(subject="msq-prov-1", email="prov1@x.de"))
        assert u.id is not None
        assert u.msq_user_id == "msq-prov-1"
        assert u.is_admin is False


def test_admin_email_allowlist_grants_admin(monkeypatch):
    monkeypatch.setattr(
        "app.identity.get_settings",
        lambda: _settings_with(admin_emails={"chief@mindsquare.de"}),
    )
    with SessionLocal() as db:
        u = resolve_user(db, _principal(subject="msq-admin-1", email="chief@mindsquare.de"))
        assert u.is_admin is True


def test_apphub_admin_role_grants_admin():
    with SessionLocal() as db:
        u = resolve_user(
            db, _principal(subject="msq-admin-2", email="ops@x.de",
                           roles=frozenset({"AppHub.Admin"}))
        )
        assert u.is_admin is True


def test_existing_user_matched_by_email_and_backfilled():
    with SessionLocal() as db:
        from app.models import User

        db.add(User(email="legacy@x.de", full_name="Legacy", is_active=True))
        db.commit()
        u = resolve_user(db, _principal(subject="msq-legacy-1", email="legacy@x.de"))
        assert u.msq_user_id == "msq-legacy-1"  # backfilled, no duplicate row


def _settings_with(admin_emails):
    class _S:
        admin_email_set = admin_emails
    return _S()
