# Relay

[![tests](https://github.com/nikithpusarla/relay/actions/workflows/tests.yml/badge.svg)](https://github.com/nikithpusarla/relay/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791)
![Redis](https://img.shields.io/badge/Redis-7-DC382D)

**An idempotent webhook gateway and reconciliation service for Stripe.**

Relay sits between Stripe and a merchant's app. It makes sure every Stripe event reaches the app **exactly once in practice**, **parents before children**, even when the app is down for an hour or the webhook never arrived at all.

## The problem

Stripe webhooks are reliable, but they come with three caveats every integration has to handle:

| Stripe's behaviour | What breaks in a naive app | What Relay does |
|---|---|---|
| **At-least-once** delivery: the same event can arrive more than once | A customer gets two confirmation emails, or a refund is booked twice | Dedupes on the Stripe event id (`evt_…`), the table's primary key |
| **No ordering guarantee**: `charge.refunded` can arrive before `charge.succeeded` | The app tries to refund a charge it has never seen | Holds child events until their parent is delivered (with a deadline) |
| **Retries eventually stop**: if your endpoint is down too long, events are gone | Silent data loss | Retries with backoff, keeps a dead-letter queue, and **reconciles hourly** against Stripe's Events API |

Relay takes those three problems off the merchant's hands: the app just receives clean, deduplicated, correctly ordered events.

## Architecture

```mermaid
flowchart LR
    Stripe(["Stripe"])
    subgraph Relay
        API["Relay API<br/>FastAPI<br/>verify signature + store"]
        DB[("PostgreSQL<br/>events table<br/>PK = evt_id")]
        Worker["Worker(s)<br/>claim, deliver,<br/>retry, dead-letter"]
        Recon["Reconciler<br/>hourly"]
        Redis[("Redis<br/>reconcile-lock")]
        Admin["Admin API<br/>/events, /replay, /stats"]
    end
    Merchant(["Merchant app"])

    Stripe -- "1. signed webhook" --> API
    API -- "2. INSERT (duplicate = ignored)" --> DB
    API -. "3. 200 only after commit" .-> Stripe
    Worker -- "4. SELECT ... FOR UPDATE SKIP LOCKED" --> DB
    Worker -- "5. POST + Relay-Event-Id" --> Merchant
    Recon -- "SET NX EX 600" --> Redis
    Recon -- "Events API: list since checkpoint" --> Stripe
    Recon -- "insert missing events" --> DB
    Admin --- DB
```

Relay is four small processes that only talk to each other through the database:

| Component | File | Job |
|---|---|---|
| **API** | [app/main.py](app/main.py) | `POST /webhooks/stripe`: verify, store, answer `200`. Plus the admin API. |
| **Signature check** | [app/signature.py](app/signature.py) | HMAC-SHA256 over `"{t}." + raw body`, written by hand |
| **Worker** | [app/worker.py](app/worker.py) | Claims due events, holds children, delivers, retries, dead-letters |
| **Delivery** | [app/delivery.py](app/delivery.py) | HTTP send with httpx (5 s timeout) and backoff maths |
| **Ordering rules** | [app/ordering.py](app/ordering.py) | Which event types must wait for which parent |
| **Reconciler** | [app/reconcile.py](app/reconcile.py) | Hourly catch-up from Stripe's Events API, behind a Redis lock |
| **Models / DB** | [app/models.py](app/models.py), [app/db.py](app/db.py) | `events` and `checkpoints` tables (SQLAlchemy 2.x) |
| **Config** | [app/config.py](app/config.py) | All settings from environment variables (pydantic-settings) |

## Workflow: the life of one event

```mermaid
sequenceDiagram
    autonumber
    participant S as Stripe
    participant A as Relay API
    participant D as Database
    participant W as Worker
    participant M as Merchant app

    S->>A: POST /webhooks/stripe (Stripe-Signature header)
    A->>A: verify HMAC-SHA256, timestamp within 300 s
    alt bad signature
        A-->>S: 400 (nothing stored)
    else valid
        A->>D: INSERT event (status = pending)
        Note over A,D: duplicate evt_id gives IntegrityError, ignored
        A-->>S: 200 (only after the row is committed)
    end

    loop every second
        W->>D: claim due events, oldest first, batch 50, SKIP LOCKED
        alt child event whose parent is not delivered yet (and < 600 s old)
            W->>D: hold: recheck in 5 s, attempts unchanged
        else ready
            W->>M: POST /events (Relay-Event-Id)
            alt 2xx
                W->>D: status = delivered
            else error, timeout or 5xx
                W->>D: attempts + 1, next_attempt_at = now + backoff
            end
        end
    end
```

### Event status

```mermaid
stateDiagram-v2
    [*] --> pending: webhook or reconciliation
    pending --> pending: merchant error, retry with backoff
    pending --> pending: waiting for parent (no attempt used)
    pending --> delivered: merchant answers 2xx
    pending --> dead: 12th failed attempt
    dead --> pending: POST /events/{id}/replay
    delivered --> [*]
```

### Reconciliation (hourly)

```mermaid
flowchart TD
    A["Start"] --> B{"Redis: SET reconcile-lock NX EX 600"}
    B -- "already held" --> X["Exit: another run is active"]
    B -- "acquired" --> C["since = checkpoint (default: now - 3 days) - 5 min overlap"]
    C --> D["fetch_events(since): stripe.Event.list(created gte since).auto_paging_iter()"]
    D --> E["Insert each event Relay doesn't have yet, source = reconciliation"]
    E --> F["Save new checkpoint (only after a full successful pass)"]
    F --> G["Release lock (only if it's still ours)"]
    G --> H["Print: recovered N missing event(s)"]
```

Recovered events go into the same `events` table as `pending`, so the worker delivers them exactly like webhooks, with the same dedupe, ordering and retries.

## Features

- **Hand-written signature verification.** Parses `Stripe-Signature`, computes HMAC-SHA256 over `"{t}." + raw body`, enforces a 300 s tolerance, compares with `hmac.compare_digest`, and accepts any matching `v1` value (so secret rotation works). Bad signature → `400`.
- **Store before acknowledge.** The event is committed to the database *before* Relay returns `200`.
- **Dedupe on event id.** `evt_…` is the primary key, so the database rejects duplicates. Relay still answers them with `200`.
- **Worker.** Batches of 50, oldest Stripe event first, `FOR UPDATE SKIP LOCKED` on PostgreSQL so several workers can run in parallel safely. Every request carries a `Relay-Event-Id` header so the merchant can dedupe too.
- **Retries + dead-letter queue.** Delay = `min(3600, 2 · 2^(n-1))` seconds plus 0–10 % jitter. After 12 failures (about 68 minutes of retrying) the event is marked `dead`. `POST /events/{id}/replay` sends it again.
- **Out-of-order handling.** For example, `charge.refunded` waits for its `charge.succeeded`, for at most 600 s. Waiting doesn't use up retry attempts.

  | Child event | Waits for | Parent id taken from |
  |---|---|---|
  | `charge.refunded` | `charge.succeeded` | `data.object.id` |
  | `refund.created`, `charge.refund.updated`, `charge.dispute.created` | `charge.succeeded` | `data.object.charge` |
  | `invoice.paid`, `invoice.payment_failed` | `invoice.created` | `data.object.id` |
  | `customer.subscription.updated`, `customer.subscription.deleted` | `customer.subscription.created` | `data.object.id` |

- **Hourly reconciliation.** Redis lock (`SET NX EX 600`), checkpoint plus 5-minute overlap, Stripe Events API with auto-pagination, missing events inserted with `source=reconciliation`.
- **Admin API.** `GET /events?status=&limit=`, `POST /events/{id}/replay`, `GET /stats` (counts per status, how many came from reconciliation, average received → delivered time). Optional bearer token via `ADMIN_TOKEN`.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework / server | FastAPI, Uvicorn |
| Database | PostgreSQL 16 (SQLite for tests and quick local runs), SQLAlchemy 2.x |
| Lock | Redis 7 |
| Stripe | `stripe` Python library (Events API), Stripe CLI for local testing |
| HTTP client | httpx |
| Config | pydantic-settings (`.env`, see [.env.example](.env.example)) |
| Tests / CI | pytest, GitHub Actions with a real PostgreSQL service |
| Packaging | Docker, Docker Compose |

## Project structure

```
relay/
├── app/
│   ├── config.py        # settings from environment variables
│   ├── db.py            # engine + session
│   ├── models.py        # events, checkpoints
│   ├── signature.py     # Stripe-Signature verification
│   ├── ordering.py      # parent/child dependency map
│   ├── main.py          # FastAPI: webhook + admin API
│   ├── delivery.py      # HTTP sender + backoff
│   ├── worker.py        # claim → hold / deliver / retry / dead-letter
│   └── reconcile.py     # hourly Events API catch-up
├── demo/
│   ├── merchant.py      # fake merchant on :9000 (can be switched down)
│   └── send_events.py   # signed fake events: duplicates, refund before charge
├── tests/               # 64 tests, no network, no Stripe
├── .github/workflows/tests.yml
├── Dockerfile
└── docker-compose.yml   # postgres, redis, api, worker, reconciler, merchant
```

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
stripe listen --print-secret                # prints whsec_… → put it in .env as STRIPE_WEBHOOK_SECRET
stripe listen --all-snapshot --forward-to localhost:8000/webhooks/stripe
stripe trigger charge.refunded              # 7 real events: charge, payment intent, refund, ...
```

`--all-snapshot` is required on Stripe CLI 1.55+. Relay reads full "snapshot" payloads (`data.object.charge` etc.), not thin events.

Reconciliation demo: set `STRIPE_API_KEY=sk_test_…` in `.env`, start Redis (`docker compose up -d redis`), stop `stripe listen`, run `stripe trigger charge.refunded` (those events never reach Relay), then run `python -m app.reconcile --once`. It prints `recovered 7 missing event(s)`, and the worker delivers them. Running it again prints `recovered 0`, because dedupe by event id makes reconciliation safe to repeat.

### Tests

```bash
pytest -q -rP                                     # SQLite, no network, no Stripe
TEST_POSTGRES_URL=postgresql+psycopg://relay:relay@localhost:5433/relay_test pytest -q   # + real SKIP LOCKED test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the full suite on every push, against a real PostgreSQL service.

## Results

### Live run against Stripe test mode

Real Stripe events, real signatures, forwarded with the Stripe CLI:

| Check | Result |
|---|---|
| `stripe trigger charge.refunded` through the webhook | 7 events, all signatures valid (7 × `200`, 0 × `400`), 7 delivered, 0 duplicates, average received → delivered **1.4 s** |
| Ordering | `charge.succeeded` reached the merchant before `charge.refunded`, `refund.created` and `charge.refund.updated` |
| Webhook tunnel cut, then `stripe trigger charge.refunded` | Relay received nothing |
| `python -m app.reconcile --once` | **`recovered 7 missing event(s)`**, all delivered. Merchant: 15 unique, 0 duplicates |
| Reconcile again | `recovered 0`: the overlap and re-reads are harmless |
| Lock already held in Redis | `another run holds the lock, exiting` |

### Automated tests

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

### Failure injection

From [tests/test_outage.py](tests/test_outage.py), all through the real webhook endpoint and worker:

```
OUTAGE TEST: 200 unique events + 40 duplicates during a 30-min outage;
             delivered=200, duplicates delivered=0, lost=0, dead=0
DLQ TEST:    200 unique + 40 duplicates during a 2-hour outage;
             200 dead-lettered then replayed; delivered=200, duplicates delivered=0, lost=0
```

Both traffic mixes include 40 charge/refund pairs in shuffled arrival order. Every refund reached the merchant after its charge.

The 30-minute outage heals with retries alone, with no human involved. The 2-hour outage is longer than the ~68-minute retry window, so events move to the dead-letter queue, where they wait safely until an operator replays them. `MAX_ATTEMPTS` and `BACKOFF_BASE_SECONDS` tune that window without code changes. (Stripe retries for up to 3 days; on top of that, Relay's reconciler is the backstop.)

## Design decisions

**Idempotency via primary key.** "Check if it exists, then insert" has a race: two copies arriving together both pass the check. Making `evt_id` the primary key lets the database be the referee. The second insert fails with `IntegrityError`, and we answer `200` anyway so Stripe stops retrying.

**Store before acknowledge.** Stripe takes a `2xx` as "delivered, never send again". If Relay replied `200` and then crashed before saving, the event would be gone forever. Saving first means a crash leads to a missing `200`, so Stripe retries and nothing is lost.

**At-least-once + dedupe = exactly once in practice.** True exactly-once delivery over a network is impossible: if the merchant processes an event and the connection drops before its `200`, Relay can't tell whether it worked. Relay retries, so delivery is at-least-once, and every request carries `Relay-Event-Id` so the merchant can ignore repeats.

**SKIP LOCKED.** Like cashiers at a deli counter: each takes the next ticket nobody else is holding instead of queueing behind a colleague. Workers lock their batch with `FOR UPDATE` and skip rows another worker has locked, so they never block each other or double-deliver. The whole batch is one transaction, so locks are held until delivery results are committed. SQLite has no row locks, so the clause is only added on PostgreSQL.

**Backoff + jitter.** Doubling gives a struggling merchant room to recover; the 1-hour cap bounds the wait. Jitter stops 1,000 events that failed together from all retrying in the same second (thundering herd).

**Parent rules with a timeout.** A refund delivered before its charge confuses most apps, so children wait for their parent. The wait has a deadline (600 s) because the parent may never arrive, for example when the merchant isn't subscribed to that type. Without the deadline a child could be held forever. Holding doesn't consume retry attempts.

**Reconciliation with overlap and a lock.** Webhooks can be lost if Relay is down longer than Stripe's retry window. Stripe's Events API is the source of truth. Each run reads from the last checkpoint minus 5 minutes, so events created at the edge of the previous run aren't missed, and dedupe makes the overlap harmless. A Redis `SET NX EX` lock keeps two reconcilers from running at once, and its expiry frees the lock if a holder crashes. Release only deletes our own token (Lua check-and-delete). The checkpoint only advances after a full successful pass.

**Injectable time and network.** `worker.run_once(session, sender, now)` and `reconcile.run(session, fetch_events, lock, now)` take their clock, HTTP sender, Stripe client and lock as arguments. Tests can simulate a 30-minute outage in milliseconds with no `sleep`, no network, and no Stripe account.

## What I'd do next

- **Bulk replay** (`POST /events/replay?status=dead`) and auto-replay when the merchant recovers.
- **Leases instead of long transactions**: mark claimed rows `processing` with a lease expiry, so delivery doesn't hold DB locks for up to `batch × timeout`.
- **Wake children immediately** when a parent is delivered, instead of rechecking every 5 s.
- **Filter reconciliation by subscribed event types** (`types=[…]` on the Events API) so merchants only get what they asked for.
- **Alembic migrations**, metrics (Prometheus: queue depth, delivery latency, DLQ size), and structured logs.
- **Per-merchant routing** with separate secrets and downstream URLs.
