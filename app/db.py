"""Database engine, sessions, and time helpers.

All timestamps are stored as *naive UTC* datetimes. SQLite does not keep
timezone info, so mixing aware and naive datetimes would crash comparisons.
One rule everywhere ("naive = UTC") keeps SQLite and PostgreSQL identical.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    """Create an engine, with the extra options SQLite needs."""
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # FastAPI may use a session from a different thread than the one that
        # opened it; SQLite refuses that unless we turn the check off.
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            # An in-memory DB lives inside one connection, so every session
            # must share that single connection or each would see an empty DB.
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind: Engine = engine) -> None:
    """Create tables if they don't exist (simple stand-in for migrations)."""
    import app.models  # noqa: F401  (registers the tables on Base)

    Base.metadata.create_all(bind)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with SessionLocal() as session:
        yield session


def is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_epoch(dt: datetime) -> int:
    """Naive-UTC datetime -> Unix seconds (Stripe's time format)."""
    return int(dt.replace(tzinfo=UTC).timestamp())
