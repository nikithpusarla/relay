"""Which events must wait for another event to be delivered first.

Stripe does not guarantee delivery order. A merchant that gets
"charge.refunded" before "charge.succeeded" may not know the charge yet.
So each child event type names its parent event type, plus the field in
data.object that holds the parent's object id.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rule:
    parent_type: str
    parent_id_field: str  # field inside data.object


DEPENDENCIES: dict[str, Rule] = {
    # data.object IS the charge
    "charge.refunded": Rule("charge.succeeded", "id"),
    # data.object is a refund or dispute that points at its charge
    "refund.created": Rule("charge.succeeded", "charge"),
    "charge.refund.updated": Rule("charge.succeeded", "charge"),
    "charge.dispute.created": Rule("charge.succeeded", "charge"),
    # data.object IS the invoice
    "invoice.paid": Rule("invoice.created", "id"),
    "invoice.payment_failed": Rule("invoice.created", "id"),
    # data.object IS the subscription
    "customer.subscription.updated": Rule("customer.subscription.created", "id"),
    "customer.subscription.deleted": Rule("customer.subscription.created", "id"),
}


def parent_of(event_type: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return (parent_event_type, parent_object_id), or None if no parent is needed."""
    rule = DEPENDENCIES.get(event_type)
    if rule is None:
        return None
    obj = (payload.get("data") or {}).get("object") or {}
    parent_id = obj.get(rule.parent_id_field)
    if isinstance(parent_id, dict):  # Stripe can "expand" ids into full objects
        parent_id = parent_id.get("id")
    if not isinstance(parent_id, str) or not parent_id:
        return None
    return rule.parent_type, parent_id
