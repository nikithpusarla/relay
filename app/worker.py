"""The worker: picks up due events and delivers them to the merchant.

Run it with:  python -m app.worker
You can run several copies at once; SKIP LOCKED (PostgreSQL) makes sure two
workers never grab the same event.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, init_db, is_postgres, utcnow
from app.delivery import backoff, send
from app.models import Event, Status
from app.ordering import parent_of

Sender = Callable[[Event], tuple[bool, str | None]]


@dataclass
class RunResult:
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    waiting: int = 0

    @property
    def claimed(self) -> int:
        return self.delivered + self.retried + self.dead + self.waiting


def claim_query(now: datetime, limit: int, postgres: bool) -> Select:
    """Due events, oldest Stripe event first.

    FOR UPDATE locks the rows we pick. SKIP LOCKED means: if another worker
    already locked a row, don't wait for it, just take the next free one.
    SQLite has no row locks (it locks the whole file), so we skip the clause.
    """
    stmt = (
        select(Event)
        .where(Event.status == Status.PENDING, Event.next_attempt_at <= now)
        .order_by(Event.stripe_created, Event.received_at, Event.id)
        .limit(limit)
    )
    if postgres:
        stmt = stmt.with_for_update(skip_locked=True)
    return stmt


def parent_delivered(session: Session, parent_type: str, parent_object_id: str) -> bool:
    found = session.scalar(
        select(Event.id)
        .where(
            Event.type == parent_type,
            Event.object_id == parent_object_id,
            Event.status == Status.DELIVERED,
        )
        .limit(1)
    )
    return found is not None


def should_wait_for_parent(session: Session, event: Event, now: datetime) -> bool:
    """True if the event's parent hasn't been delivered and we're still willing to wait.

    The wait has a deadline: if the parent never arrives (Stripe lost it, or
    the merchant isn't subscribed to it) we'd otherwise hold the child forever.
    """
    parent = parent_of(event.type, event.payload)
    if parent is None:
        return False
    if now - event.received_at >= timedelta(seconds=settings.parent_wait_seconds):
        return False
    return not parent_delivered(session, *parent)


def hold(event: Event, now: datetime) -> None:
    """Check again soon, without counting it as a failed attempt."""
    deadline = event.received_at + timedelta(seconds=settings.parent_wait_seconds)
    event.next_attempt_at = min(now + timedelta(seconds=settings.parent_recheck_seconds), deadline)


def record_success(event: Event, now: datetime) -> None:
    event.attempts += 1
    event.status = Status.DELIVERED
    event.delivered_at = now
    event.last_error = None


def record_failure(event: Event, error: str | None, now: datetime) -> bool:
    """Schedule a retry, or dead-letter the event. Returns True if it is now dead."""
    event.attempts += 1
    event.last_error = error
    if event.attempts >= settings.max_attempts:
        event.status = Status.DEAD
        return True
    event.next_attempt_at = now + timedelta(seconds=backoff(event.attempts))
    return False


def run_once(session: Session, sender: Sender, now: datetime) -> RunResult:
    """Process one batch of due events.

    Everything happens in ONE transaction that commits at the end. On
    PostgreSQL that keeps our row locks for the whole batch, so no other
    worker can grab these events while we're delivering them. If we crash
    mid-batch, nothing is committed and the events are delivered again later:
    that's "at-least-once", and the merchant dedupes with Relay-Event-Id.
    """
    result = RunResult()
    stmt = claim_query(now, settings.worker_batch_size, is_postgres(session))
    events = list(session.scalars(stmt))

    for event in events:
        if should_wait_for_parent(session, event, now):
            hold(event, now)
            result.waiting += 1
            continue
        try:
            ok, error = sender(event)
        except Exception as exc:  # a bug in a sender must not kill the batch
            ok, error = False, f"{type(exc).__name__}: {exc}"
        if ok:
            record_success(event, now)
            # Flush so a child later in this same batch sees its parent as delivered.
            session.flush()
            result.delivered += 1
        elif record_failure(event, error, now):
            result.dead += 1
        else:
            result.retried += 1

    session.commit()
    return result


def main() -> None:
    init_db()
    print(f"worker started, delivering to {settings.downstream_url}", flush=True)
    with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:

        def sender(event: Event) -> tuple[bool, str | None]:
            return send(event, client=client)

        while True:
            try:
                with SessionLocal() as session:
                    result = run_once(session, sender, utcnow())
            except Exception as exc:  # e.g. database briefly unavailable
                print(f"worker error: {exc!r}", flush=True)
                time.sleep(settings.worker_poll_seconds)
                continue
            if result.claimed:
                print(
                    f"delivered={result.delivered} retried={result.retried} "
                    f"dead={result.dead} waiting={result.waiting}",
                    flush=True,
                )
            # A full batch means there's probably more work: go again at once.
            if result.claimed < settings.worker_batch_size:
                time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
