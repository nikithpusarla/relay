# Relay: project rules for Claude Code

You are helping me build "Relay", a portfolio project for my Stripe Software Engineer Intern application. I will explain every line in interviews, so teach as you build.

## PROJECT
Relay is an idempotent webhook gateway and reconciliation service that sits between Stripe and a merchant's app. It guarantees every Stripe event reaches the merchant's app exactly once in practice (at-least-once delivery + idempotency), handles out-of-order events, survives merchant outages, and recovers missed events.

## FEATURES (all must exist and be tested; these are claims on my resume)
1. POST /webhooks/stripe: verify the Stripe-Signature header by hand (HMAC-SHA256 over f"{t}." + raw body, 300-second tolerance, hmac.compare_digest, support multiple v1 values). Save the event BEFORE returning 200. Bad signature -> 400.
2. Dedupe: the Stripe event id (evt_...) is the primary key of the events table. A duplicate insert (IntegrityError) is ignored and still returns 200.
3. Worker: claims due events (status=pending AND next_attempt_at <= now, oldest stripe_created first, batch 50) using SELECT ... FOR UPDATE SKIP LOCKED on PostgreSQL (skip that clause on SQLite). Delivers to DOWNSTREAM_URL with httpx (5s timeout) and header Relay-Event-Id. 2xx = delivered.
4. Retries: exponential backoff delay = min(cap, base * 2**(attempts-1)) + up to 10% random jitter (base 2s, cap 3600s). After MAX_ATTEMPTS=12 failures -> status dead (dead-letter). (Changed from 8 on 2026-11-13: 8 attempts only covered ~4.5 min of downtime; 12 covers ~68 min.) Admin replay resets to pending, attempts=0.
5. Out-of-order handling: a dependency map (child type -> parent type + how to get the parent's object id). Examples: charge.refunded -> charge.succeeded (data.object.id); refund.created and charge.refund.updated and charge.dispute.created -> charge.succeeded (data.object.charge); invoice.paid and invoice.payment_failed -> invoice.created; customer.subscription.updated/deleted -> customer.subscription.created. A child waits (without counting as a failed attempt) until its parent is delivered, or until PARENT_WAIT_SECONDS=600 have passed since it was received, then it is delivered anyway.
6. Reconciliation (hourly): take a Redis lock (SET reconcile-lock NX EX 600; exit if held), read checkpoint (default now - 3 days), go back 5 extra minutes for overlap, list events from Stripe's Events API (stripe.Event.list(created={"gte": since}, limit=100).auto_paging_iter()), insert any missing ones with source="reconciliation", save the checkpoint, release the lock, print how many were recovered. The Stripe call must be behind a function fetch_events(since) that tests can replace.
7. Admin API: GET /events?status=..., POST /events/{id}/replay, GET /stats (counts per status, count from reconciliation, average received->delivered seconds).
8. Demo: demo/merchant.py (FastAPI on port 9000, POST /events records ids and counts duplicates, POST /toggle switches up/down; down returns 503) and demo/send_events.py (sends signed fake events including duplicates and a refund before its charge).

## TECH STACK (must match my resume exactly)
Python 3.11+, FastAPI, Uvicorn, PostgreSQL (SQLite allowed for tests and quick local runs), SQLAlchemy 2.x, Redis, stripe Python library, httpx, pydantic-settings, pytest, Docker + Docker Compose, GitHub Actions.

## FOLDER STRUCTURE
```
app/{__init__,config,db,models,signature,ordering,main,delivery,worker,reconcile}.py
demo/{merchant,send_events}.py
tests/{conftest,test_signature,test_webhook,test_delivery,test_ordering,test_reconcile,test_outage}.py
.github/workflows/tests.yml, Dockerfile, docker-compose.yml, requirements.txt, .env.example, .gitignore, README.md
```

## DATABASE
- events: id (str PK), type, object_id, stripe_created (int), payload (JSON), source (webhook|reconciliation), status (pending|delivered|dead), attempts (int), next_attempt_at, last_error, received_at, delivered_at. Indexes on status, next_attempt_at, type, object_id.
- checkpoints: key (str PK), value (int).

## DESIGN RULES
- Small, readable functions with type hints and short docstrings explaining WHY, not what.
- No secrets in code. Settings come from environment variables via pydantic-settings; provide .env.example; .env in .gitignore.
- Make time and network injectable: worker.run_once(session, sender, now) and reconcile.run(session, fetch_events, lock, now) so tests need no sleep and no real network.
- Tests never call Stripe or the internet.
- Keep it simple. No extra frameworks, no Celery, no Kafka, no ORM magic I can't explain.

## HOW WE WORK (important)
- Build ONE step at a time, only when I send the step. Don't jump ahead.
- After each step: (1) list the files you created or changed, (2) explain the key logic in simple English as if teaching a student, (3) tell me the exact commands to run it and its tests, (4) suggest a git commit message. Then stop and wait.
- If I ask "why", explain with a simple real-world analogy.
- If something on my resume can't be supported by the code, tell me.
