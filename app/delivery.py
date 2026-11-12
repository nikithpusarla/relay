"""Sending one event to the merchant, and how long to wait after a failure."""

import random
from collections.abc import Callable

import httpx

from app.config import settings
from app.models import Event


def send(
    event: Event,
    url: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[bool, str | None]:
    """POST the event to the merchant. Returns (ok, error).

    Never raises for network problems: a timeout or refused connection is
    just a failed attempt that the worker will retry.
    """
    target = url or settings.downstream_url
    headers = {"Relay-Event-Id": event.id}  # lets the merchant dedupe too
    try:
        if client is not None:
            resp = client.post(target, json=event.payload, headers=headers)
        else:
            resp = httpx.post(
                target,
                json=event.payload,
                headers=headers,
                timeout=settings.delivery_timeout_seconds,
            )
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"HTTP {resp.status_code}"


def backoff(
    attempts: int,
    rng: Callable[[], float] = random.random,
    base: float | None = None,
    cap: float | None = None,
) -> float:
    """Seconds to wait before the next try, after `attempts` failures.

    Doubling gives a struggling merchant room to recover. The cap stops waits
    growing forever. Jitter (0-10% extra) spreads retries out, so 1,000 events
    that failed together don't all retry in the same second.
    """
    base = settings.backoff_base_seconds if base is None else base
    cap = settings.backoff_cap_seconds if cap is None else cap
    exponent = min(max(attempts - 1, 0), 32)  # 2**32 * base is already past any cap
    delay = min(cap, base * 2**exponent)
    return delay + delay * 0.1 * rng()
