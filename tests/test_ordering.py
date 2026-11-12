"""Out-of-order events: unit tests for parent_of, then worker-level behaviour."""

import pytest

from app.models import Event, Status
from app.ordering import DEPENDENCIES, parent_of
from app.worker import run_once

# ---------- parent_of ----------


@pytest.mark.parametrize(
    "event_type, obj, expected",
    [
        ("charge.refunded", {"id": "ch_1"}, ("charge.succeeded", "ch_1")),
        ("refund.created", {"id": "re_1", "charge": "ch_1"}, ("charge.succeeded", "ch_1")),
        ("charge.refund.updated", {"id": "re_1", "charge": "ch_1"}, ("charge.succeeded", "ch_1")),
        ("charge.dispute.created", {"id": "dp_1", "charge": "ch_1"}, ("charge.succeeded", "ch_1")),
        ("invoice.paid", {"id": "in_1"}, ("invoice.created", "in_1")),
        ("invoice.payment_failed", {"id": "in_1"}, ("invoice.created", "in_1")),
        ("customer.subscription.updated", {"id": "sub_1"}, ("customer.subscription.created", "sub_1")),
        ("customer.subscription.deleted", {"id": "sub_1"}, ("customer.subscription.created", "sub_1")),
    ],
)
def test_parent_of_known_types(event_type, obj, expected):
    assert parent_of(event_type, {"data": {"object": obj}}) == expected


def test_every_rule_is_covered_above():
    assert len(DEPENDENCIES) == 8


def test_parent_of_unrelated_type_is_none():
    assert parent_of("charge.succeeded", {"data": {"object": {"id": "ch_1"}}}) is None
    assert parent_of("customer.created", {"data": {"object": {"id": "cus_1"}}}) is None


def test_parent_of_missing_field_is_none():
    assert parent_of("refund.created", {"data": {"object": {"id": "re_1"}}}) is None
    assert parent_of("charge.refunded", {}) is None


def test_parent_of_expanded_object():
    payload = {"data": {"object": {"id": "dp_1", "charge": {"id": "ch_9", "object": "charge"}}}}
    assert parent_of("charge.dispute.created", payload) == ("charge.succeeded", "ch_9")


# ---------- worker behaviour ----------


def charge(event_factory, n=1):
    return event_factory(f"evt_charge_{n}", "charge.succeeded", {"id": f"ch_{n}"}, created=1000)


def refund(event_factory, n=1):
    return event_factory(f"evt_refund_{n}", "charge.refunded", {"id": f"ch_{n}"}, created=1010)


def test_refund_first_is_held(session, add_event, event_factory, clock, merchant):
    row = add_event(refund(event_factory))

    result = run_once(session, merchant, clock.now)

    assert result.waiting == 1
    assert merchant.received == []
    assert row.status == Status.PENDING
    assert row.attempts == 0  # waiting is not a failure
    assert row.next_attempt_at > clock.now


def test_refund_released_after_charge_delivered(session, add_event, event_factory, clock, merchant):
    add_event(refund(event_factory))
    run_once(session, merchant, clock.now)  # refund held

    clock.advance(2)
    add_event(charge(event_factory))
    run_once(session, merchant, clock.now)  # charge delivered; refund not due yet
    clock.advance(10)
    run_once(session, merchant, clock.now)  # refund rechecked and released

    assert merchant.received == ["evt_charge_1", "evt_refund_1"]


def test_parent_and_child_in_same_batch(session, add_event, event_factory, clock, merchant):
    add_event(refund(event_factory))  # arrives first, but created later
    add_event(charge(event_factory))

    result = run_once(session, merchant, clock.now)

    assert result.delivered == 2
    assert merchant.received == ["evt_charge_1", "evt_refund_1"]


def test_delivered_anyway_after_600s_if_parent_never_comes(session, add_event, event_factory, clock, merchant):
    add_event(refund(event_factory))
    for _ in range(200):  # simulate ~10 minutes of polling every 5s
        run_once(session, merchant, clock.now)
        if merchant.received:
            break
        clock.advance(5)

    assert merchant.received == ["evt_refund_1"]
    row = session.get(Event, "evt_refund_1")
    assert row.attempts == 1  # all that waiting never counted as failures
    assert (row.delivered_at - row.received_at).total_seconds() >= 600


def test_child_waits_for_correct_parent_only(session, add_event, event_factory, clock, merchant):
    add_event(charge(event_factory, n=1))
    add_event(refund(event_factory, n=2))  # refund for a DIFFERENT charge

    result = run_once(session, merchant, clock.now)

    assert merchant.received == ["evt_charge_1"]
    assert result.waiting == 1
