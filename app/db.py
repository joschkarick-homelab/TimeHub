from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # check_same_thread off: FastAPI hands the connection across threads.
        # timeout: block (not error) up to 30s when another writer holds the lock.
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


engine = _make_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    # Only SQLite needs these; Postgres connections are left untouched.
    if engine.url.drivername.startswith("sqlite"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")     # concurrent readers + one writer
        cur.execute("PRAGMA busy_timeout=30000")   # wait instead of "database is locked"
        cur.execute("PRAGMA foreign_keys=ON")      # enforce FKs (off by default in SQLite)
        cur.execute("PRAGMA synchronous=NORMAL")   # safe + fast under WAL
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
