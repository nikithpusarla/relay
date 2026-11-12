"""Shared test fixtures. Every test gets a brand-new in-memory database."""

import os

# Set these BEFORE importing the app, so settings never pick up a real
# database or secret from the developer's .env file.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret"
os.environ["STRIPE_API_KEY"] = ""
os.environ["ADMIN_TOKEN"] = ""
# Pin the numbers the tests reason about, whatever is in .env.
os.environ.update(
    MAX_ATTEMPTS="12",
    BACKOFF_BASE_SECONDS="2",
    BACKOFF_CAP_SECONDS="3600",
    PARENT_WAIT_SECONDS="600",
    PARENT_RECHECK_SECONDS="5",
    WORKER_BATCH_SIZE="50",
    RECONCILE_DEFAULT_LOOKBACK_SECONDS="259200",
    RECONCILE_OVERLAP_SECONDS="300",
    RECONCILE_LOCK_TTL_SECONDS="600",
)

import json  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db import Base, get_session, make_engine, utcnow  # noqa: E402
from app.main import app, get_now  # noqa: E402
from app.models import Event, insert_event  # noqa: E402
from app.signature import make_header  # noqa: E402

SECRET = "whsec_test_secret"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
    engine.dispose()


class Clock:
    """A fake clock tests can move forward by hand."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, seconds: float) -> datetime:
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(utcnow().replace(microsecond=0))


@pytest.fixture
def client(session: Session, clock: Clock) -> Iterator[TestClient]:
    # Dependency injection: swap the real DB session and clock for test ones.
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_now] = lambda: clock.now
    yield TestClient(app)
    app.dependency_overrides.clear()


def make_event(
    event_id: str,
    event_type: str = "payment_intent.succeeded",
    obj: dict[str, Any] | None = None,
    created: int = 1_700_000_000,
) -> dict[str, Any]:
    """A minimal Stripe-shaped event."""
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {"object": obj or {"id": f"obj_{event_id}"}},
    }


@pytest.fixture
def event_factory() -> Callable[..., dict[str, Any]]:
    return make_event


@pytest.fixture
def post_event(client: TestClient):
    """Sign an event like Stripe would and POST it to the webhook endpoint."""

    def _post(event: dict[str, Any], secret: str = SECRET):
        body = json.dumps(event).encode()
        return client.post(
            "/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": make_header(body, secret), "Content-Type": "application/json"},
        )

    return _post


@pytest.fixture
def add_event(session: Session, clock: Clock):
    """Insert an event straight into the DB (skipping HTTP) and return the row."""

    def _add(event: dict[str, Any], source: str = "webhook") -> Event:
        assert insert_event(session, event, source, clock.now)
        return session.get(Event, event["id"])

    return _add


class FakeMerchant:
    """Stands in for the merchant's app: can be up or down, records what it got."""

    def __init__(self) -> None:
        self.up = True
        self.received: list[str] = []

    def __call__(self, event: Event) -> tuple[bool, str | None]:
        if not self.up:
            return False, "HTTP 503"
        self.received.append(event.id)
        return True, None


@pytest.fixture
def merchant() -> FakeMerchant:
    return FakeMerchant()
