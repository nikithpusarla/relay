"""Reconciliation: ask Stripe "what happened?" and fill in events we missed.

Webhooks can be lost (Relay was down longer than Stripe kept retrying, a
misconfigured endpoint, etc.). Stripe's Events API is the source of truth,
so once an hour we list recent events and insert any we don't have.

Run it with:  python -m app.reconcile          (loops forever)
              python -m app.reconcile --once   (one pass, handy for demos)
"""

import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, init_db, to_epoch, utcnow
from app.models import Checkpoint, Event, Source, insert_event

CHECKPOINT_KEY = "reconcile"
LOCK_KEY = "reconcile-lock"

FetchEvents = Callable[[int], Iterable[dict[str, Any]]]


def fetch_events(since: int) -> Iterator[dict[str, Any]]:
    """All Stripe events created at or after `since` (Unix seconds).

    auto_paging_iter() follows Stripe's pagination for us, 100 per page.
    Tests pass their own fake instead of this, so they never touch Stripe.
    """
    import stripe

    stripe.api_key = settings.stripe_api_key
    for evt in stripe.Event.list(created={"gte": since}, limit=100).auto_paging_iter():
        yield evt.to_dict(for_json=True)


class Lock(Protocol):
    def acquire(self) -> bool: ...
    def release(self) -> None: ...


class RedisLock:
    """A lock shared by every reconciler process, so only one runs at a time.

    SET key token NX EX ttl = "set only if it doesn't exist, and expire it
    after ttl seconds". The expiry means a crashed process can't hold the
    lock forever. The random token makes sure we only delete OUR lock, not
    one another process took after ours expired.
    """

    _RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    def __init__(self, client: Any, key: str = LOCK_KEY, ttl: int | None = None) -> None:
        self.client = client
        self.key = key
        self.ttl = ttl or settings.reconcile_lock_ttl_seconds
        self.token = uuid.uuid4().hex

    def acquire(self) -> bool:
        return bool(self.client.set(self.key, self.token, nx=True, ex=self.ttl))

    def release(self) -> None:
        # Check-and-delete must be atomic, hence a tiny Lua script.
        self.client.eval(self._RELEASE, 1, self.key, self.token)


class InMemoryLock:
    """Same behaviour as RedisLock, for tests. Share `store` to simulate two processes."""

    def __init__(self, store: dict[str, str] | None = None, key: str = LOCK_KEY) -> None:
        self.store = {} if store is None else store
        self.key = key
        self.token = uuid.uuid4().hex

    def acquire(self) -> bool:
        if self.key in self.store:
            return False
        self.store[self.key] = self.token
        return True

    def release(self) -> None:
        if self.store.get(self.key) == self.token:
            del self.store[self.key]


def run(session: Session, fetch_events: FetchEvents, lock: Lock, now: datetime) -> int | None:
    """One reconciliation pass. Returns events recovered, or None if another run holds the lock."""
    if not lock.acquire():
        print("reconcile: another run holds the lock, exiting")
        return None
    try:
        checkpoint = session.get(Checkpoint, CHECKPOINT_KEY)
        last = checkpoint.value if checkpoint else to_epoch(now) - settings.reconcile_default_lookback_seconds
        # Go back a few extra minutes. An event created right at the end of the
        # previous run might not have been listed yet; overlap catches it, and
        # dedupe makes re-reading the same events harmless.
        since = last - settings.reconcile_overlap_seconds

        recovered = 0
        for data in fetch_events(since):
            if session.get(Event, data["id"]) is not None:
                continue
            if insert_event(session, data, Source.RECONCILIATION, now):
                recovered += 1

        # Only move the checkpoint after the whole list succeeded. If Stripe
        # errors halfway, the next run starts from the old checkpoint again.
        if checkpoint is None:
            session.add(Checkpoint(key=CHECKPOINT_KEY, value=to_epoch(now)))
        else:
            checkpoint.value = to_epoch(now)
        session.commit()
        print(f"reconcile: recovered {recovered} missing event(s) since {since}")
        return recovered
    finally:
        lock.release()


def main() -> None:
    import redis

    init_db()
    once = "--once" in sys.argv
    client = redis.Redis.from_url(settings.redis_url)
    while True:
        if not settings.stripe_api_key:
            print("reconcile: STRIPE_API_KEY not set, skipping")
        else:
            try:
                with SessionLocal() as session:
                    run(session, fetch_events, RedisLock(client), utcnow())
            except Exception as exc:
                print(f"reconcile error: {exc!r}")
        if once:
            return
        time.sleep(settings.reconcile_interval_seconds)


if __name__ == "__main__":
    main()
