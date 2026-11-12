"""Fire signed fake Stripe events at Relay, including the nasty cases.

    python demo/send_events.py [relay_url]

Sends: normal events, duplicates of them, and a refund BEFORE its charge.
Uses STRIPE_WEBHOOK_SECRET from your environment / .env, like Relay does.
"""

import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app` imports work

from app.config import settings  # noqa: E402
from app.signature import make_header  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/webhooks/stripe"


def event(event_type: str, obj: dict, created: int) -> dict:
    return {
        "id": f"evt_demo_{uuid.uuid4().hex[:12]}",
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {"object": obj},
    }


def post(evt: dict, label: str) -> None:
    body = json.dumps(evt).encode()
    headers = {"Stripe-Signature": make_header(body, settings.stripe_webhook_secret), "Content-Type": "application/json"}
    resp = httpx.post(URL, content=body, headers=headers, timeout=5)
    print(f"{label:<28} {evt['type']:<26} {evt['id']}  -> {resp.status_code} {resp.text}")


def main() -> None:
    if not settings.stripe_webhook_secret:
        sys.exit("Set STRIPE_WEBHOOK_SECRET (in .env) to the same value Relay uses.")
    now = int(time.time())
    run = uuid.uuid4().hex[:6]

    plain = [event("payment_intent.succeeded", {"id": f"pi_{run}_{i}"}, now - 60 + i) for i in range(3)]
    for e in plain:
        post(e, "normal")
    for e in plain[:2]:
        post(e, "DUPLICATE (Stripe retry)")

    charge_id = f"ch_{run}"
    charge = event("charge.succeeded", {"id": charge_id, "amount": 2000}, now - 30)
    refund = event("charge.refunded", {"id": charge_id, "amount_refunded": 2000}, now - 10)
    post(refund, "OUT OF ORDER: refund first")
    print("   ...waiting 3s so the worker sees the refund alone and holds it")
    time.sleep(3)
    post(charge, "charge arrives late")
    post(charge, "DUPLICATE charge")

    print("\nExpected at the merchant: 5 unique events, 0 duplicates, charge BEFORE refund.")
    print("Check:  curl http://localhost:9000/stats   and   curl http://localhost:8000/stats")


if __name__ == "__main__":
    main()
