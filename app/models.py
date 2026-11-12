"""Tables and the single function that inserts a Stripe event."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base, to_epoch


class Status:
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD = "dead"
    ALL = (PENDING, DELIVERED, DEAD)


class Source:
    WEBHOOK = "webhook"
    RECONCILIATION = "reconciliation"


class Event(Base):
    __tablename__ = "events"

    # Stripe's own event id. Making it the primary key means the database
    # itself rejects a second copy, even if two requests race each other.
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(255), index=True)
    object_id: Mapped[str | None] = mapped_column(String(255), index=True)
    stripe_created: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default=Source.WEBHOOK)
    status: Mapped[str] = mapped_column(String(32), default=Status.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)


class Checkpoint(Base):
    """Small key/value table; reconciliation stores 'last run' time here."""

    __tablename__ = "checkpoints"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)


def insert_event(session: Session, data: dict[str, Any], source: str, now: datetime) -> bool:
    """Insert a Stripe event. Returns False if we already had it.

    We don't "check then insert" because two copies arriving at the same time
    could both pass the check. Instead we just insert and let the primary key
    reject the duplicate (IntegrityError).
    """
    obj = (data.get("data") or {}).get("object") or {}
    event = Event(
        id=data["id"],
        type=data["type"],
        object_id=obj.get("id"),
        stripe_created=int(data.get("created") or to_epoch(now)),
        payload=data,
        source=source,
        status=Status.PENDING,
        attempts=0,
        next_attempt_at=now,
        received_at=now,
    )
    session.add(event)
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False
