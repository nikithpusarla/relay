"""Backoff maths, the HTTP sender, and the worker's retry / dead-letter logic."""

import os
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.config import settings
from app.delivery import backoff, send
from app.models import Event, Status
from app.worker import claim_query, run_once

# ---------- backoff ----------


def test_backoff_doubles_without_jitter():
    no_jitter = lambda: 0.0  # noqa: E731
    assert [backoff(n, rng=no_jitter) for n in range(1, 8)] == [2, 4, 8, 16, 32, 64, 128]


def test_backoff_is_capped():
    no_jitter = lambda: 0.0  # noqa: E731
    assert backoff(12, rng=no_jitter) == 3600  # 2 * 2**11 = 4096 -> capped
    assert backoff(10_000, rng=no_jitter) == 3600  # huge attempt counts don't overflow


def test_backoff_jitter_is_at_most_ten_percent():
    assert backoff(3, rng=lambda: 1.0) == pytest.approx(8.8)
    assert backoff(12, rng=lambda: 1.0) == pytest.approx(3960)
    for _ in range(100):
        assert 8 <= backoff(3) <= 8.8


# ---------- send() over a fake HTTP transport (no network) ----------


def fake_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def an_event() -> Event:
    return Event(id="evt_1", type="charge.succeeded", payload={"id": "evt_1"})


def test_send_2xx_is_success_and_sets_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.headers["Relay-Event-Id"]
        seen["body"] = request.content
        return httpx.Response(204)

    ok, err = send(an_event(), url="http://merchant/events", client=fake_client(handler))
    assert (ok, err) == (True, None)
    assert seen["id"] == "evt_1"
    assert b"evt_1" in seen["body"]


def test_send_5xx_is_failure():
    ok, err = send(an_event(), url="http://m/e", client=fake_client(lambda r: httpx.Response(503)))
    assert (ok, err) == (False, "HTTP 503")


def test_send_timeout_is_failure_not_exception():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    ok, err = send(an_event(), url="http://m/e", client=fake_client(handler))
    assert ok is False
    assert "ReadTimeout" in err


# ---------- worker retries ----------


def always_fail(event):
    return False, "HTTP 500"


def test_success_marks_delivered(session, add_event, event_factory, clock, merchant):
    row = add_event(event_factory("evt_1"))
    result = run_once(session, merchant, clock.now)

    assert result.delivered == 1
    assert row.status == Status.DELIVERED
    assert row.delivered_at == clock.now
    assert row.attempts == 1
    # Delivered events are never picked up again.
    assert run_once(session, merchant, clock.advance(10_000)).claimed == 0
    assert merchant.received == ["evt_1"]


def test_failure_increments_attempts_and_moves_next_attempt(session, add_event, event_factory, clock):
    row = add_event(event_factory("evt_1"))
    result = run_once(session, always_fail, clock.now)

    assert result.retried == 1
    assert row.status == Status.PENDING
    assert row.attempts == 1
    assert row.last_error == "HTTP 500"
    delay = (row.next_attempt_at - clock.now).total_seconds()
    assert 2 <= delay <= 2.2
    # Not due yet, so an immediate second run does nothing.
    assert run_once(session, always_fail, clock.now).claimed == 0


def test_max_failures_goes_dead(session, add_event, event_factory, clock):
    row = add_event(event_factory("evt_1"))
    for _ in range(settings.max_attempts - 1):
        run_once(session, always_fail, clock.now)
        clock.advance(3600 * 2)  # jump past any backoff
    assert row.status == Status.PENDING  # one attempt left

    run_once(session, always_fail, clock.now)
    assert row.status == Status.DEAD
    assert row.attempts == settings.max_attempts == 12
    # Dead events stay put: the worker never claims them again.
    assert run_once(session, always_fail, clock.advance(10**6)).claimed == 0


def test_sender_exception_counts_as_failure(session, add_event, event_factory, clock):
    def broken(event):
        raise RuntimeError("boom")

    row = add_event(event_factory("evt_1"))
    assert run_once(session, broken, clock.now).retried == 1
    assert "RuntimeError" in row.last_error


def test_oldest_stripe_event_first_and_batch_limit(session, add_event, event_factory, clock, merchant, monkeypatch):
    monkeypatch.setattr(settings, "worker_batch_size", 2)
    add_event(event_factory("evt_new", created=300))
    add_event(event_factory("evt_old", created=100))
    add_event(event_factory("evt_mid", created=200))

    assert run_once(session, merchant, clock.now).delivered == 2
    assert merchant.received == ["evt_old", "evt_mid"]


# ---------- SKIP LOCKED ----------


def test_claim_query_uses_skip_locked_on_postgres_only():
    stmt = claim_query(datetime(2026, 1, 1), 50, postgres=True)
    assert "FOR UPDATE SKIP LOCKED" in str(stmt.compile(dialect=postgresql.dialect()))
    stmt = claim_query(datetime(2026, 1, 1), 50, postgres=False)
    assert "FOR UPDATE" not in str(stmt.compile(dialect=sqlite.dialect()))


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL")
def test_two_workers_never_claim_the_same_rows_on_postgres():
    """Real PostgreSQL: worker A holds locks on 5 rows; worker B must skip them."""
    from sqlalchemy import delete
    from sqlalchemy.orm import sessionmaker

    from app.db import Base, make_engine, utcnow
    from app.models import insert_event

    engine = make_engine(os.environ["TEST_POSTGRES_URL"])
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = utcnow()
    with factory() as setup:
        setup.execute(delete(Event))
        setup.commit()
        for i in range(10):
            insert_event(setup, {"id": f"evt_{i}", "type": "t", "created": i, "data": {"object": {}}}, "webhook", now)

    a, b = factory(), factory()
    try:
        claimed_a = {e.id for e in a.scalars(claim_query(now + timedelta(seconds=1), 5, postgres=True))}
        claimed_b = {e.id for e in b.scalars(claim_query(now + timedelta(seconds=1), 10, postgres=True))}
        assert len(claimed_a) == 5
        assert len(claimed_b) == 5
        assert claimed_a.isdisjoint(claimed_b)
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()
        with factory() as cleanup:
            cleanup.execute(delete(Event))
            cleanup.commit()
        engine.dispose()
