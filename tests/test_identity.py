from sqlalchemy import func, select

from app.db import SessionLocal
from app.identity import HUB_PLACEHOLDER_DOMAIN, HubPrincipal, _apply_existing, resolve_user
from app.models import User


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
        db.add(User(email="legacy@x.de", full_name="Legacy", is_active=True))
        db.commit()
        u = resolve_user(db, _principal(subject="msq-legacy-1", email="legacy@x.de"))
        assert u.msq_user_id == "msq-legacy-1"  # backfilled
        assert u.id is not None
        # Prove no duplicate row was created.
        assert db.execute(
            select(func.count()).select_from(User).where(
                func.lower(User.email) == "legacy@x.de"
            )
        ).scalar_one() == 1


def test_apply_existing_backfills_and_grants():
    """The reconciliation helper used by both the normal and race-loser paths:
    it backfills the Hub subject and grants admin, but leaves full_name alone
    (provision-only — the in-app profile is authoritative)."""
    with SessionLocal() as db:
        u = User(email="helper@x.de", full_name="Old", is_active=True)
        db.add(u)
        db.commit()
        out = _apply_existing(
            db, u,
            _principal(subject="msq-helper-1", email="helper@x.de", name="New",
                       roles=frozenset({"AppHub.Admin"})),
        )
        assert out.msq_user_id == "msq-helper-1"
        # full_name is NOT overwritten from the Hub name on an existing user.
        assert out.full_name == "Old"
        assert out.is_admin is True


def test_full_name_is_provision_only_not_overwritten():
    """The Hub display name seeds full_name on provision; resolving the SAME
    subject again with a different Hub name must NOT overwrite it (the in-app
    profile edit wins)."""
    subject = "msq-name-prov-1"
    with SessionLocal() as db:
        u = resolve_user(db, _principal(subject=subject, email="alpha@x.de", name="Alpha"))
        assert u.full_name == "Alpha"  # provision sets it
    with SessionLocal() as db:
        u2 = resolve_user(db, _principal(subject=subject, email="alpha@x.de", name="Beta"))
        assert u2.full_name == "Alpha"  # later Hub name does NOT overwrite


def test_resolve_twice_same_subject_is_idempotent():
    """Re-resolving a brand-new subject must not create a second row."""
    with SessionLocal() as db:
        resolve_user(db, _principal(subject="msq-idem-1", email="idem@x.de"))
    with SessionLocal() as db:
        u2 = resolve_user(db, _principal(subject="msq-idem-1", email="idem@x.de"))
        assert u2.msq_user_id == "msq-idem-1"
        assert db.execute(
            select(func.count()).select_from(User).where(
                User.msq_user_id == "msq-idem-1"
            )
        ).scalar_one() == 1


def test_provision_race_loser_returns_winner_row():
    """First-touch commit hits the UNIQUE constraint (a competitor inserted the
    same subject first); resolve_user must recover and return that single row."""
    subject = "msq-race-1"
    # The competitor (race winner) inserts the row in a separate session.
    with SessionLocal() as winner_db:
        winner = User(
            email="race@x.de", full_name="Winner", msq_user_id=subject,
            is_active=True,
        )
        winner_db.add(winner)
        winner_db.commit()
        winner_id = winner.id

    # The loser's select missed (simulate by clearing identity map), so it tries
    # to INSERT and the real DB UNIQUE constraint raises IntegrityError. We force
    # the missed select by patching the first lookup to return None.
    with SessionLocal() as loser_db:
        calls = {"n": 0}
        real_execute = loser_db.execute

        def fake_execute(stmt, *a, **kw):
            # The first two calls are the pre-INSERT lookups (msq_user_id, then
            # email) in resolve_user → force both to miss so the loser proceeds
            # to INSERT and trips the UNIQUE constraint. The post-rollback
            # recovery selects (calls 3+) run for real and find the winner.
            calls["n"] += 1
            if calls["n"] <= 2:
                class _Empty:
                    def scalar_one_or_none(self_inner):
                        return None
                return _Empty()
            return real_execute(stmt, *a, **kw)

        loser_db.execute = fake_execute  # type: ignore[method-assign]
        try:
            out = resolve_user(
                loser_db,
                _principal(subject=subject, email="race@x.de", name="Loser"),
            )
        finally:
            loser_db.execute = real_execute  # type: ignore[method-assign]
        assert out.id == winner_id

    # Exactly one row exists for that subject.
    with SessionLocal() as db:
        assert db.execute(
            select(func.count()).select_from(User).where(
                User.msq_user_id == subject
            )
        ).scalar_one() == 1


def test_placeholder_email_backfilled_with_real_email():
    """No-email principal gets an @hub.local placeholder; a later real email
    replaces it, with no duplicate row."""
    subject = "msq-noemail-1"
    with SessionLocal() as db:
        u = resolve_user(db, _principal(subject=subject, email=None, name="NoMail"))
        assert u.email.endswith(HUB_PLACEHOLDER_DOMAIN)
    with SessionLocal() as db:
        u2 = resolve_user(db, _principal(subject=subject, email="real@x.de", name="NoMail"))
        assert u2.email == "real@x.de"
        assert db.execute(
            select(func.count()).select_from(User).where(
                User.msq_user_id == subject
            )
        ).scalar_one() == 1


def _settings_with(admin_emails):
    class _S:
        admin_email_set = admin_emails
    return _S()
