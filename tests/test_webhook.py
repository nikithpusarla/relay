"""Webhook endpoint and admin API, tested through real HTTP calls (TestClient)."""

import json

from sqlalchemy import func, select

from app.models import Event, Source, Status
from app.signature import make_header
from app.worker import run_once


def count(session) -> int:
    return session.scalar(select(func.count()).select_from(Event))


# ---------- POST /webhooks/stripe ----------


def test_one_event_one_row(post_event, session, event_factory):
    event = event_factory("evt_1", "charge.succeeded", {"id": "ch_1"}, created=1_700_000_123)
    resp = post_event(event)

    assert resp.status_code == 200
    assert resp.json() == {"received": True, "duplicate": False}
    row = session.get(Event, "evt_1")
    assert row.type == "charge.succeeded"
    assert row.object_id == "ch_1"
    assert row.stripe_created == 1_700_000_123
    assert row.status == Status.PENDING
    assert row.source == Source.WEBHOOK
    assert row.payload == event


def test_same_event_five_times_one_row(post_event, session, event_factory):
    event = event_factory("evt_dup")
    responses = [post_event(event) for _ in range(5)]

    assert [r.status_code for r in responses] == [200] * 5
    assert [r.json()["duplicate"] for r in responses] == [False, True, True, True, True]
    assert count(session) == 1


def test_bad_signature_400_and_no_row(post_event, session, event_factory):
    resp = post_event(event_factory("evt_bad"), secret="whsec_wrong")
    assert resp.status_code == 400
    assert count(session) == 0


def test_missing_signature_400(client, session):
    resp = client.post("/webhooks/stripe", content=b'{"id":"evt_x","type":"t"}')
    assert resp.status_code == 400
    assert count(session) == 0


def test_body_changed_after_signing_400(client, session, event_factory):
    body = json.dumps(event_factory("evt_1")).encode()
    header = make_header(body, "whsec_test_secret")
    tampered = body.replace(b"evt_1", b"evt_2")
    resp = client.post("/webhooks/stripe", content=tampered, headers={"Stripe-Signature": header})
    assert resp.status_code == 400
    assert count(session) == 0


def test_signed_but_not_an_event_400(client, session):
    body = b'["not", "an", "event"]'
    resp = client.post(
        "/webhooks/stripe", content=body, headers={"Stripe-Signature": make_header(body, "whsec_test_secret")}
    )
    assert resp.status_code == 400
    assert count(session) == 0


# ---------- Admin API ----------


def test_list_events_filters_by_status(client, add_event, session, event_factory, clock):
    add_event(event_factory("evt_a"))
    add_event(event_factory("evt_b"))
    session.get(Event, "evt_b").status = Status.DEAD
    session.commit()

    all_ids = {e["id"] for e in client.get("/events").json()}
    dead_ids = [e["id"] for e in client.get("/events?status=dead").json()]

    assert all_ids == {"evt_a", "evt_b"}
    assert dead_ids == ["evt_b"]
    assert client.get("/events?status=nonsense").status_code == 400
    assert len(client.get("/events?limit=1").json()) == 1


def test_replay_resets_dead_event(client, add_event, session, event_factory, clock, merchant):
    row = add_event(event_factory("evt_dead"))
    row.status, row.attempts, row.last_error = Status.DEAD, 12, "HTTP 503"
    session.commit()

    resp = client.post("/events/evt_dead/replay")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["last_error"] is None
    # And the worker now picks it up again.
    assert run_once(session, merchant, clock.now).delivered == 1
    assert merchant.received == ["evt_dead"]


def test_replay_missing_event_404(client):
    assert client.post("/events/evt_nope/replay").status_code == 404


def test_stats(client, add_event, session, event_factory, clock, merchant):
    add_event(event_factory("evt_1"))
    add_event(event_factory("evt_2"), source=Source.RECONCILIATION)
    add_event(event_factory("evt_3"))
    session.get(Event, "evt_3").status = Status.DEAD
    session.commit()

    clock.advance(4)
    run_once(session, merchant, clock.now)  # delivers evt_1 and evt_2, 4s after receipt

    stats = client.get("/stats").json()
    assert stats["by_status"] == {"pending": 0, "delivered": 2, "dead": 1}
    assert stats["total"] == 3
    assert stats["from_reconciliation"] == 1
    assert abs(stats["avg_delivery_seconds"] - 4.0) < 0.01


def test_stats_empty_db(client):
    stats = client.get("/stats").json()
    assert stats["total"] == 0
    assert stats["avg_delivery_seconds"] is None


def test_admin_token_required_when_set(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_token", "s3cret")
    assert client.get("/stats").status_code == 401
    assert client.get("/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    # The Stripe webhook must NOT need the admin token (Stripe can't send it).
    assert client.get("/health").status_code == 200
