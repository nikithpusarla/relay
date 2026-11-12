"""HTTP API: the Stripe webhook endpoint plus a small admin API.

Run it with:  uvicorn app.main:app --port 8000
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, init_db, utcnow
from app.models import Event, Source, Status, insert_event
from app.signature import SignatureError, verify


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Relay", lifespan=lifespan)


def get_now() -> datetime:
    """Dependency for "the current time", so tests can control the clock."""
    return utcnow()


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """If ADMIN_TOKEN is set, admin routes need 'Authorization: Bearer <token>'."""
    if settings.admin_token and authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Verify, store, THEN acknowledge.

    Stripe treats a 2xx as "you've got it, I won't send it again". So we must
    only answer 200 once the event is safely in the database. If we crash
    before that, Stripe gets no 200 and retries, so nothing is lost.
    """
    raw = await request.body()  # raw bytes, exactly as signed
    try:
        verify(
            raw,
            request.headers.get("Stripe-Signature"),
            settings.stripe_webhook_secret,
            settings.signature_tolerance_seconds,
        )
    except SignatureError as exc:
        raise HTTPException(status_code=400, detail=f"invalid signature: {exc}") from exc

    try:
        data = json.loads(raw)
        if not isinstance(data.get("id"), str) or not isinstance(data.get("type"), str):
            raise ValueError("missing id or type")
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="invalid event body") from exc

    # DB calls are blocking; run them off the event loop so other requests keep flowing.
    inserted = await run_in_threadpool(insert_event, session, data, Source.WEBHOOK, now)
    # A duplicate still gets 200: we already have it, so Stripe should stop retrying.
    return {"received": True, "duplicate": not inserted}


def event_to_dict(e: Event) -> dict[str, Any]:
    def iso(dt: datetime | None) -> str | None:
        return dt.isoformat() + "Z" if dt else None

    return {
        "id": e.id,
        "type": e.type,
        "object_id": e.object_id,
        "status": e.status,
        "source": e.source,
        "attempts": e.attempts,
        "last_error": e.last_error,
        "stripe_created": e.stripe_created,
        "received_at": iso(e.received_at),
        "next_attempt_at": iso(e.next_attempt_at),
        "delivered_at": iso(e.delivered_at),
    }


@app.get("/events", dependencies=[Depends(require_admin)])
def list_events(
    status: str | None = None,
    limit: int = 50,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    if status is not None and status not in Status.ALL:
        raise HTTPException(status_code=400, detail=f"status must be one of {Status.ALL}")
    limit = max(1, min(limit, 1000))
    stmt = select(Event).order_by(Event.received_at.desc(), Event.id).limit(limit)
    if status is not None:
        stmt = stmt.where(Event.status == status)
    return [event_to_dict(e) for e in session.scalars(stmt)]


@app.post("/events/{event_id}/replay", dependencies=[Depends(require_admin)])
def replay_event(
    event_id: str,
    session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Send an event again from scratch, e.g. a dead one after the merchant fixed a bug."""
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    event.status = Status.PENDING
    event.attempts = 0
    event.next_attempt_at = now
    event.last_error = None
    event.delivered_at = None
    session.commit()
    return event_to_dict(event)


def avg_delivery_seconds(session: Session) -> float | None:
    """Average received -> delivered time. Date maths differs per database."""
    if session.get_bind().dialect.name == "postgresql":
        diff = func.extract("epoch", Event.delivered_at - Event.received_at)
    else:  # SQLite: julianday() is in days
        diff = (func.julianday(Event.delivered_at) - func.julianday(Event.received_at)) * 86400.0
    value = session.scalar(select(func.avg(diff)).where(Event.status == Status.DELIVERED))
    return round(float(value), 3) if value is not None else None


@app.get("/stats", dependencies=[Depends(require_admin)])
def stats(session: Session = Depends(get_session)) -> dict[str, Any]:
    counts = {s: 0 for s in Status.ALL}
    for status, count in session.execute(select(Event.status, func.count()).group_by(Event.status)):
        counts[status] = count
    from_reconciliation = session.scalar(
        select(func.count()).select_from(Event).where(Event.source == Source.RECONCILIATION)
    )
    return {
        "by_status": counts,
        "total": sum(counts.values()),
        "from_reconciliation": from_reconciliation or 0,
        "avg_delivery_seconds": avg_delivery_seconds(session),
    }
