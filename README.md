# Relay

**An idempotent webhook gateway and reconciliation service for Stripe.**

Stripe delivers webhooks *at least once*, *in no guaranteed order*, and gives up if your app is down too long.
Relay sits between Stripe and a merchant's app so the app gets every event once in practice, parents before children, even across outages.
It also checks Stripe's Events API every hour to recover anything that never arrived.

> Demo video: _coming soon_ (link)

## Architecture

```
                 signed webhook                       POST + Relay-Event-Id
  ┌────────┐   ───────────────▶  ┌──────────────┐                    ┌──────────────┐
  │ Stripe │                     │  Relay API   │                    │  Merchant    │
  │        │   ◀─── 200 ───────  │ verify+store │                    │    app       │
  └────────┘   (only after the   └──────┬───────┘                    └──────▲───────┘
      ▲          row is saved)          │ INSERT (PK = evt_id)              │
      │                                 ▼                                   │
      │                          ┌──────────────┐   SELECT … FOR UPDATE     │
      │  Events API (hourly)     │  PostgreSQL  │◀── SKIP LOCKED ──┐        │
      │                          │ events table │                  │        │
      │                          └──────▲───────┘           ┌──────┴──────┐ │
      │                                 │ insert missing    │  Worker(s)  │─┘
      │                          ┌──────┴───────┐           │ retry, DLQ, │
      └───────────────────────── │  Reconciler  │           │ parent wait │
                                 │ (Redis lock) │           └─────────────┘
                                 └──────────────┘
```

| Component | File | Job |
|---|---|---|
| API | [app/main.py](app/main.py) | `POST /webhooks/stripe`, admin API |
| Signature check | [app/signature.py](app/signature.py) | HMAC-SHA256, written by hand |
| Worker | [app/worker.py](app/worker.py) | claims due events, delivers, retries, dead-letters |
| Ordering rules | [app/ordering.py](app/ordering.py) | which event types must wait for a parent |
| Delivery | [app/delivery.py](app/delivery.py) | HTTP send + backoff maths |
| Reconciler | [app/reconcile.py](app/reconcile.py) | hourly catch-up from Stripe's Events API |

## Features

- **Signature verification**: parses `Stripe-Signature`, HMAC-SHA256 over `"{t}." + raw body`, 300 s tolerance, `hmac.compare_digest`, accepts any matching `v1` (secret rotation). Bad signature → `400`.
- **Store before acknowledge**: the event is committed to the database *before* Relay returns `200`.
- **Dedupe on event id**: `evt_…` is the primary key, so duplicates are rejected by the database and still answered with `200`.
- **Worker**: batches of 50, oldest Stripe event first, `FOR UPDATE SKIP LOCKED` on PostgreSQL so many workers can run in parallel safely. Sends a `Relay-Event-Id` header so the merchant can dedupe too.
- **Retries + dead-letter queue**: `min(3600, 2 · 2^(n-1))` seconds plus 0–10 % jitter. After 12 failures (about 68 minutes of retrying) the event is marked `dead`. `POST /events/{id}/replay` sends it again.
- **Out-of-order handling**: e.g. `charge.refunded` waits until its `charge.succeeded` is delivered, for up to 600 s. Waiting doesn't count as a failed attempt.
- **Hourly reconciliation**: Redis lock (`SET NX EX 600`), checkpoint plus a 5-minute overlap, `stripe.Event.list(...).auto_paging_iter()`, inserts missing events with `source=reconciliation`.
- **Admin API**: `GET /events?status=&limit=`, `POST /events/{id}/replay`, `GET /stats`. Optional bearer token via `ADMIN_TOKEN`.

## Running it

### Local (SQLite, three terminals)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                    # then set STRIPE_WEBHOOK_SECRET to any value

python demo/merchant.py                 # terminal 1: fake merchant on :9000
uvicorn app.main:app --port 8000        # terminal 2: Relay API
python -m app.worker                    # terminal 3: Relay worker

python demo/send_events.py              # fire signed events: duplicates + refund-before-charge
curl localhost:9000/stats               # merchant: 5 unique, 0 duplicates, charge before refund
curl localhost:8000/stats               # relay: 5 delivered
```

Outage demo: `curl -X POST localhost:9000/toggle` (merchant returns 503), send events, watch the worker log retries, toggle again, and watch them all arrive once.

### Docker Compose (PostgreSQL + Redis)

```bash
docker compose up --build               # postgres, redis, api, worker, reconciler, merchant
python demo/send_events.py              # from the host, same as above
docker compose logs -f worker
```

Postgres is published on host port **5433** so it doesn't clash with a local install.

### Real Stripe test mode

```bash
stripe login
stripe listen --forward-to localhost:8000/webhooks/stripe   # prints whsec_… → put it in .env
stripe trigger payment_intent.succeeded
stripe trigger charge.refunded
```

Reconciliation demo: set `STRIPE_API_KEY=sk_test_…` in `.env`, stop `stripe listen`, trigger a few events (they never reach Relay), then run `python -m app.reconcile --once`. It prints `recovered N missing event(s)`, and the worker delivers them.

### Tests

```bash
pytest -q -rP                                     # SQLite, no network, no Stripe
TEST_POSTGRES_URL=postgresql+psycopg://relay:relay@localhost:5433/relay_test pytest -q   # + real SKIP LOCKED test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the full suite on every push, against a real PostgreSQL service.

## Design decisions

**Idempotency via primary key.** "Check if it exists, then insert" has a race: two copies arriving together both pass the check. Making `evt_id` the primary key lets the database be the referee. The second insert fails with `IntegrityError`, and we answer `200` anyway so Stripe stops retrying.

**Store before acknowledge.** Stripe takes a `2xx` as "delivered, never send again". If Relay replied `200` and then crashed before saving, the event would be gone forever. Saving first means a crash leads to a missing `200`, so Stripe retries and nothing is lost.

**At-least-once + dedupe = exactly once in practice.** True exactly-once delivery over a network is impossible: if the merchant processes an event and the connection drops before its `200`, Relay can't tell whether it worked. Relay retries, so delivery is at-least-once, and every request carries `Relay-Event-Id` so the merchant can ignore repeats.

**SKIP LOCKED.** Like cashiers at a deli counter: each takes the next ticket nobody else is holding instead of queueing behind a colleague. Workers lock their batch with `FOR UPDATE` and skip rows another worker has locked, so they never block each other or double-deliver. The whole batch is one transaction, so locks are held until delivery results are committed. SQLite has no row locks, so the clause is only added on PostgreSQL.

**Backoff + jitter.** Doubling gives a struggling merchant room to recover; the 1-hour cap bounds the wait. Jitter stops 1,000 events that failed together from all retrying in the same second (thundering herd).

**Parent rules with a timeout.** A refund delivered before its charge confuses most apps, so children wait for their parent. The wait has a deadline (600 s) because the parent may never arrive, for example when the merchant isn't subscribed to that type. Without the deadline a child could be held forever. Holding doesn't consume retry attempts.

**Reconciliation with overlap and a lock.** Webhooks can be lost if Relay is down longer than Stripe's retry window. Stripe's Events API is the source of truth. Each run reads from the last checkpoint minus 5 minutes, so events created at the edge of the previous run aren't missed, and dedupe makes the overlap harmless. A Redis `SET NX EX` lock keeps two reconcilers from running at once, and its expiry frees the lock if a holder crashes. Release only deletes our own token (Lua check-and-delete). The checkpoint only advances after a full successful pass.

**Injectable time and network.** `worker.run_once(session, sender, now)` and `reconcile.run(session, fetch_events, lock, now)` take their clock, HTTP sender, Stripe client and lock as arguments. Tests can simulate a 30-minute outage in milliseconds with no `sleep`, no network, and no Stripe account.

## Test results

**64 tests, all passing** (63 on SQLite + 1 against real PostgreSQL; without `TEST_POSTGRES_URL` that one is skipped).

| Area | What's covered |
|---|---|
| Signatures | valid, tampered body, wrong secret, expired timestamp, missing/malformed header, multiple `v1` |
| Webhook | one event → one row; same event ×5 → five `200`s, one row; bad signature → `400`, no row |
| Delivery | backoff values, cap, jitter bounds; 2xx/5xx/timeout handling; retry scheduling; 12 failures → dead |
| Ordering | every dependency rule; refund held; released after charge; delivered anyway after 600 s |
| Reconciliation | Stripe has 10, we have 7 → 3 recovered; second run adds 0; lock held → no-op; overlap maths |
| Concurrency | two PostgreSQL sessions claim disjoint rows with `SKIP LOCKED` |
| Admin | list/filter, replay, 404, stats, auth token |

**Failure-injection results** ([tests/test_outage.py](tests/test_outage.py)), all through the real webhook endpoint and worker:

```
OUTAGE TEST: 200 unique events + 40 duplicates during a 30-min outage;
             delivered=200, duplicates delivered=0, lost=0, dead=0
DLQ TEST:    200 unique + 40 duplicates during a 2-hour outage;
             200 dead-lettered then replayed; delivered=200, duplicates delivered=0, lost=0
```

Both traffic mixes include 40 charge/refund pairs in shuffled arrival order. Every refund reached the merchant after its charge.

The 30-minute outage heals with retries alone, with no human involved. The 2-hour outage is longer than the ~68-minute retry window, so events move to the dead-letter queue, where they wait safely until an operator replays them. `MAX_ATTEMPTS` and `BACKOFF_BASE_SECONDS` tune that window without code changes. (Stripe retries for up to 3 days; on top of that, Relay's reconciler is the backstop.)

## What I'd do next

- **Bulk replay** (`POST /events/replay?status=dead`) and auto-replay when the merchant recovers.
- **Leases instead of long transactions**: mark claimed rows `processing` with a lease expiry, so delivery doesn't hold DB locks for up to `batch × timeout`.
- **Wake children immediately** when a parent is delivered, instead of rechecking every 5 s.
- **Filter reconciliation by subscribed event types** (`types=[…]` on the Events API) so merchants only get what they asked for.
- **Alembic migrations**, metrics (Prometheus: queue depth, delivery latency, DLQ size), and structured logs.
- **Per-merchant routing** with separate secrets and downstream URLs.
