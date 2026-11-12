"""Reconciliation against a fake Stripe Events API (no network)."""

import pytest

from app.config import settings
from app.db import to_epoch
from app.models import Checkpoint, Event, Source
from app.reconcile import CHECKPOINT_KEY, InMemoryLock, run


class FakeStripe:
    """Pretends to be stripe.Event.list: returns events with created >= since."""

    def __init__(self, events):
        self.events = events
        self.calls: list[int] = []

    def __call__(self, since: int):
        self.calls.append(since)
        return [e for e in self.events if e["created"] >= since]


@pytest.fixture
def stripe_events(event_factory, clock):
    base = to_epoch(clock.now) - 3600
    return [event_factory(f"evt_{i}", created=base + i) for i in range(10)]


def test_inserts_only_missing_events(session, add_event, stripe_events, clock):
    for e in stripe_events[:7]:
        add_event(e)  # we already got these 7 via webhook

    recovered = run(session, FakeStripe(stripe_events), InMemoryLock(), clock.now)

    assert recovered == 3
    rows = session.query(Event).order_by(Event.id).all()
    assert len(rows) == 10
    recon = {r.id for r in rows if r.source == Source.RECONCILIATION}
    assert recon == {"evt_7", "evt_8", "evt_9"}
    assert all(r.status == "pending" for r in rows if r.id in recon)  # worker will deliver them


def test_running_twice_adds_nothing(session, stripe_events, clock):
    fake = FakeStripe(stripe_events)
    assert run(session, fake, InMemoryLock(), clock.now) == 10
    assert run(session, fake, InMemoryLock(), clock.advance(3600)) == 0
    assert session.query(Event).count() == 10


def test_lock_held_exits_without_changes(session, stripe_events, clock):
    store: dict[str, str] = {}
    other_process = InMemoryLock(store)
    assert other_process.acquire()
    fake = FakeStripe(stripe_events)

    result = run(session, fake, InMemoryLock(store), clock.now)

    assert result is None
    assert fake.calls == []  # never even asked Stripe
    assert session.query(Event).count() == 0
    assert session.get(Checkpoint, CHECKPOINT_KEY) is None
    assert store[other_process.key] == other_process.token  # didn't steal or delete it


def test_checkpoint_default_and_overlap(session, clock):
    fake = FakeStripe([])
    run(session, fake, InMemoryLock(), clock.now)
    first_run = to_epoch(clock.now)
    expected_first = first_run - settings.reconcile_default_lookback_seconds - settings.reconcile_overlap_seconds
    assert fake.calls == [expected_first]  # default: 3 days back, minus 5 min overlap
    assert session.get(Checkpoint, CHECKPOINT_KEY).value == first_run

    run(session, fake, InMemoryLock(), clock.advance(3600))
    assert fake.calls[1] == first_run - 300  # next run starts 5 min before the last checkpoint


def test_lock_released_even_if_stripe_fails(session, clock):
    store: dict[str, str] = {}

    def broken(since):
        raise ConnectionError("Stripe unreachable")

    with pytest.raises(ConnectionError):
        run(session, broken, InMemoryLock(store), clock.now)

    assert store == {}  # lock freed, next hour can try again
    assert session.get(Checkpoint, CHECKPOINT_KEY) is None  # checkpoint did not move


def test_recovered_events_get_delivered(session, stripe_events, clock, merchant):
    from app.worker import run_once

    run(session, FakeStripe(stripe_events), InMemoryLock(), clock.now)
    while run_once(session, merchant, clock.now).claimed:
        pass
    assert sorted(merchant.received) == sorted(e["id"] for e in stripe_events)


class FakeRedis:
    """Just enough of redis-py for RedisLock: SET NX EX and the release script."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.last_ex: int | None = None

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        self.last_ex = ex
        return True

    def eval(self, script, numkeys, key, token):
        if self.data.get(key) == token:
            del self.data[key]
            return 1
        return 0


def test_redis_lock_uses_nx_ex_and_only_releases_own_token():
    from app.reconcile import RedisLock

    r = FakeRedis()
    a, b = RedisLock(r), RedisLock(r)
    assert a.acquire() is True
    assert r.last_ex == 600
    assert b.acquire() is False
    b.release()  # b doesn't own it: must not delete a's lock
    assert "reconcile-lock" in r.data
    a.release()
    assert r.data == {}
