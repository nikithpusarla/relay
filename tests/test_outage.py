"""End-to-end failure injection: duplicates + out-of-order events + merchant outage.

Everything goes through the real webhook endpoint and the real worker. Only
the clock and the merchant are fake, so hours of "outage" take under a second.
"""

import random
from collections import Counter

from app.worker import run_once

TICK_SECONDS = 10  # how often our simulated worker wakes up


def build_traffic(event_factory):
    """200 unique events: 120 plain ones, plus 40 charges each with a refund."""
    events = [event_factory(f"evt_pi_{i}", "payment_intent.succeeded", {"id": f"pi_{i}"}, 1000 + i) for i in range(120)]
    pairs = []
    for i in range(40):
        c = event_factory(f"evt_ch_{i}", "charge.succeeded", {"id": f"ch_{i}"}, 2000 + i)
        r = event_factory(f"evt_rf_{i}", "charge.refunded", {"id": f"ch_{i}"}, 3000 + i)
        pairs.append((c, r))
        events += [c, r]
    return events, pairs


def drain(session, merchant, now):
    """What the worker loop does at one wake-up: keep going while there's due work."""
    while run_once(session, merchant, now).claimed:
        pass


def run_for(minutes, session, merchant, clock, on_tick=None):
    for tick in range(minutes * 60 // TICK_SECONDS):
        if on_tick:
            on_tick(tick)
        drain(session, merchant, clock.now)
        clock.advance(TICK_SECONDS)


def send_with_duplicates(post_event, event_factory, seed):
    """Send all events in shuffled order; return (events, pairs, dupes, status codes)."""
    rng = random.Random(seed)
    events, pairs = build_traffic(event_factory)
    rng.shuffle(events)  # arrival order is random: many refunds land before their charge
    dupes = rng.sample(events, k=len(events) // 5)  # 20% will be sent a second time
    statuses = [post_event(e).status_code for e in events]
    return events, pairs, dupes, statuses


def assert_exactly_once(merchant, events, pairs):
    counts = Counter(merchant.received)
    unique_ids = {e["id"] for e in events}
    lost = unique_ids - set(counts)
    duplicates = sum(n - 1 for n in counts.values())
    assert lost == set(), f"lost events: {sorted(lost)[:5]}"
    assert duplicates == 0
    assert set(counts) == unique_ids
    order = {eid: i for i, eid in enumerate(merchant.received)}
    for c, r in pairs:
        assert order[c["id"]] < order[r["id"]], f"{r['id']} delivered before its charge"
    return lost, duplicates


def test_30_minute_outage_recovers_by_retries_alone(client, post_event, session, clock, merchant, event_factory):
    """The headline scenario: no human involved, nothing lost, nothing doubled."""
    merchant.up = False
    events, pairs, dupes, statuses = send_with_duplicates(post_event, event_factory, seed=42)

    # 30 minutes down. Halfway through, Stripe "retries" 20% of the events.
    def stripe_retries(tick):
        if tick == 90:
            statuses.extend(post_event(e).status_code for e in dupes)

    run_for(30, session, merchant, clock, on_tick=stripe_retries)
    assert client.get("/stats").json()["by_status"] == {"pending": len(events), "delivered": 0, "dead": 0}

    merchant.up = True
    run_for(90, session, merchant, clock)  # long enough for the slowest backoff to fire

    assert statuses == [200] * (len(events) + len(dupes))
    lost, duplicates = assert_exactly_once(merchant, events, pairs)
    assert client.get("/stats").json()["by_status"] == {"pending": 0, "delivered": len(events), "dead": 0}
    print(
        f"\nOUTAGE TEST: {len(events)} unique events + {len(dupes)} duplicates during a 30-min outage; "
        f"delivered={len(set(merchant.received))}, duplicates delivered={duplicates}, lost={len(lost)}, dead=0"
    )


def test_2_hour_outage_dead_letters_then_replay(client, post_event, session, clock, merchant, event_factory):
    """Longer than the ~68-min retry window: events park in the DLQ, then an operator replays them."""
    merchant.up = False
    events, pairs, dupes, statuses = send_with_duplicates(post_event, event_factory, seed=7)
    statuses += [post_event(e).status_code for e in dupes]

    run_for(120, session, merchant, clock)
    assert statuses == [200] * (len(events) + len(dupes))
    assert client.get("/stats").json()["by_status"] == {"pending": 0, "delivered": 0, "dead": len(events)}

    merchant.up = True
    dead = client.get("/events?status=dead&limit=1000").json()
    for e in dead:
        assert client.post(f"/events/{e['id']}/replay").status_code == 200
    run_for(10, session, merchant, clock)

    lost, duplicates = assert_exactly_once(merchant, events, pairs)
    assert client.get("/stats").json()["by_status"] == {"pending": 0, "delivered": len(events), "dead": 0}
    print(
        f"\nDLQ TEST: {len(events)} unique + {len(dupes)} duplicates during a 2-hour outage; "
        f"{len(dead)} dead-lettered then replayed; delivered={len(set(merchant.received))}, "
        f"duplicates delivered={duplicates}, lost={len(lost)}"
    )
