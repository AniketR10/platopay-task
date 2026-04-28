# Playto Payout Engine

A minimal but production-shaped payout engine for the Playto Pay take-home.
Merchants accumulate balance from simulated customer credits and withdraw via
payouts to bank accounts. The hard parts — concurrency, idempotency, state
machine, and money integrity — are implemented at the database level, not in
Python.

- **Stack:** Django 5 + DRF, PostgreSQL, Django-Q2 (Postgres-broker workers, **no Redis**), React + TypeScript + Tailwind (Vite).
- **Live URL:** _add Render URL after first deploy._
- **Sharp explanation of the hard parts:** [`EXPLAINER.md`](./EXPLAINER.md).

---

## Quick start

**One command. Nothing else.** (Prerequisite: Docker + Docker Compose.)

```bash
docker compose up
```

Then open:

| | URL |
|---|---|
| Dashboard | http://localhost:5173 |
| API       | http://localhost:8000/api/v1/ |
| Health    | http://localhost:8000/healthz |

To stop: `docker compose down`. To wipe the database too: `docker compose down -v`.

> The Postgres container intentionally does **not** publish a host port, so `compose up` can't conflict with anything you already run on 5432/5433.

---

## How to evaluate this in 5 minutes

### 1. Click around the dashboard (60 seconds)
Open http://localhost:5173. You should see 3 seeded merchants. Submit a small payout (e.g. ₹500). Watch its status flip **PENDING → PROCESSING → COMPLETED** (or FAILED ~20% of the time, in which case a refund CREDIT appears in the Ledger). Try over-spending — should get a clear "insufficient funds" error.

### 2. Concurrent over-spend — only one should win (30 seconds)
```bash
M=$(curl -s http://localhost:8000/api/v1/merchants | python3 -c "import json,sys;print(json.load(sys.stdin)[2]['id'])")
B=$(curl -s http://localhost:8000/api/v1/merchants/$M | python3 -c "import json,sys;print(json.load(sys.stdin)['bank_accounts'][0]['id'])")

(curl -s -o /dev/null -w "req1: %{http_code}\n" -X POST http://localhost:8000/api/v1/payouts \
  -H "Content-Type: application/json" -H "Idempotency-Key: $(python3 -c 'import uuid;print(uuid.uuid4())')" -H "X-Merchant-Id: $M" \
  -d "{\"amount_paise\":1500000,\"bank_account_id\":\"$B\"}") &
(curl -s -o /dev/null -w "req2: %{http_code}\n" -X POST http://localhost:8000/api/v1/payouts \
  -H "Content-Type: application/json" -H "Idempotency-Key: $(python3 -c 'import uuid;print(uuid.uuid4())')" -H "X-Merchant-Id: $M" \
  -d "{\"amount_paise\":1500000,\"bank_account_id\":\"$B\"}") &
wait
```
**Expect:** one `201` and one `422`. Never two 201s.

### 3. Idempotency contract (30 seconds)
```bash
M=$(curl -s http://localhost:8000/api/v1/merchants | python3 -c "import json,sys;print(json.load(sys.stdin)[0]['id'])")
B=$(curl -s http://localhost:8000/api/v1/merchants/$M | python3 -c "import json,sys;print(json.load(sys.stdin)['bank_accounts'][0]['id'])")
K=$(python3 -c "import uuid;print(uuid.uuid4())")

# POST 1 — creates payout
curl -s -X POST http://localhost:8000/api/v1/payouts \
  -H "Content-Type: application/json" -H "Idempotency-Key: $K" -H "X-Merchant-Id: $M" \
  -d "{\"amount_paise\":100,\"bank_account_id\":\"$B\"}" | python3 -m json.tool

# POST 2 — same key, same body — returns SAME id
curl -s -X POST http://localhost:8000/api/v1/payouts \
  -H "Content-Type: application/json" -H "Idempotency-Key: $K" -H "X-Merchant-Id: $M" \
  -d "{\"amount_paise\":100,\"bank_account_id\":\"$B\"}" | python3 -m json.tool

# POST 3 — same key, different body — 409
curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8000/api/v1/payouts \
  -H "Content-Type: application/json" -H "Idempotency-Key: $K" -H "X-Merchant-Id: $M" \
  -d "{\"amount_paise\":999,\"bank_account_id\":\"$B\"}"
```
**Expect:** POST 1 + POST 2 return the same payout id; POST 3 returns 409.

### 4. Run the test suite (30 seconds)
```bash
docker compose exec web python manage.py test ledger -v 2
```
**Expect:** `Ran 7 tests in ~2s … OK`. Includes real-thread concurrency tests against real Postgres, idempotency contract tests, state machine guards, and atomic-refund verification.

### 5. The ledger invariant the spec grades on (30 seconds)
```bash
docker compose exec web python manage.py shell -c "
from ledger.models import LedgerEntry, Merchant
from ledger.services import get_balance
from django.db.models import Q, Sum
for m in Merchant.objects.all():
    a = LedgerEntry.objects.filter(merchant=m).aggregate(
        c=Sum('amount_paise', filter=Q(entry_type='CREDIT')),
        d=Sum('amount_paise', filter=Q(entry_type='DEBIT')))
    sql = (a['c'] or 0) - (a['d'] or 0)
    api = get_balance(m.id)['available_paise']
    print(f'{m.name:25s}  SQL={sql}  API={api}  match={sql==api}')
"
```
**Expect:** every line ends `match=True`. This proves `SUM(C) − SUM(D) == displayed available balance` for every merchant, after all the worker churn from the previous tests.

---

## API

### `POST /api/v1/payouts`

Creates a payout. Holds funds atomically.

| Header             | Description                                                    |
| ------------------ | -------------------------------------------------------------- |
| `Idempotency-Key`  | Required. Client-supplied string scoped per merchant. 24h TTL. |
| `X-Merchant-Id`    | Required. UUID of the merchant (stand-in for real auth).       |
| `Content-Type`     | `application/json`                                             |

Body:
```json
{ "amount_paise": 1500, "bank_account_id": "<uuid>" }
```

| Status | Meaning |
|---|---|
| **201** | Payout created (in PENDING state; worker advances it asynchronously). |
| **422** | `insufficient_funds` — available balance too low. |
| **409** | `idempotency_conflict` — same key reused with a different body, or first request didn't complete. |
| **400** | Missing header or malformed body. |

A second request with the same `Idempotency-Key` and the same body returns the **same response** (status + payload) without creating a duplicate.

### Other endpoints

| Method | Path                                | Notes                                  |
| ------ | ----------------------------------- | -------------------------------------- |
| `GET`  | `/api/v1/merchants`                 | List merchants                         |
| `GET`  | `/api/v1/merchants/<uuid>`          | Detail incl. derived balance           |
| `GET`  | `/api/v1/merchants/<uuid>/payouts`  | Last 100 payouts (newest first)        |
| `GET`  | `/api/v1/merchants/<uuid>/ledger`   | Last 100 ledger entries                |
| `GET`  | `/api/v1/payouts/<uuid>`            | Single payout                          |
| `GET`  | `/healthz`                          | Health check                           |

---

## Background workers

Django-Q2 with the Postgres ORM broker — no Redis service needed.

- `ledger.tasks.process_payout` — one settlement attempt. 70/20/10 success/fail/hang simulation, configurable.
- `ledger.tasks.retry_stuck_payouts` — scheduled every 1 minute. Re-enqueues payouts stuck in `PROCESSING` past `PAYOUT_STUCK_AFTER_SECONDS` (default 30s), with **exponential backoff** (30s, 60s, 120s). After `PAYOUT_MAX_ATTEMPTS` (default 3), forcibly fails + refunds.
- `ledger.tasks.cleanup_expired_idempotency_keys` — hourly. Drops idempotency rows past their 24h TTL.

---

## Configuration

All env vars are read by [`backend/playto/settings.py`](backend/playto/settings.py). The defaults are sensible for local dev; the spec's required behavior is enforced regardless. See [`backend/.env.example`](backend/.env.example) for the full list.

| Variable                     | Default | Purpose                                  |
| ---------------------------- | ------- | ---------------------------------------- |
| `DATABASE_URL`               | _none_  | If set, overrides `POSTGRES_*`. Used on Render. |
| `PAYOUT_SUCCESS_RATE`        | `0.70`  | Worker simulation: success probability.  |
| `PAYOUT_FAIL_RATE`           | `0.20`  | Worker simulation: fail probability.     |
| `PAYOUT_HANG_RATE`           | `0.10`  | Worker simulation: hang probability.     |
| `PAYOUT_STUCK_AFTER_SECONDS` | `30`    | When to consider a PROCESSING payout stuck. |
| `PAYOUT_MAX_ATTEMPTS`        | `3`     | Retries before giving up + refunding.    |
| `IDEMPOTENCY_TTL_HOURS`      | `24`    | Idempotency key lifetime.                |

---

## Repository layout

```
backend/        Django project + ledger app + tests
  ledger/
    models.py        Merchant, BankAccount, LedgerEntry, Payout, IdempotencyKey
    services.py      Money operations (locking, state machine, balance derivation)
    idempotency.py   Idempotency-Key handling
    tasks.py         Background workers + scheduled retry
    views.py         DRF endpoints
    tests.py         7 tests covering the four hard requirements
frontend/       React + Vite + Tailwind dashboard (2s polling)
docker-compose.yml
render.yaml     Render Blueprint for free-tier deploy
EXPLAINER.md    Answers to the 5 spec questions
```

---

## Deploying to Render (free tier)

1. Push this repo to GitHub.
2. On Render: **New → Blueprint** → connect the GitHub repo → Apply. [`render.yaml`](./render.yaml) provisions:
   - Postgres database
   - `playto-web` (Django + gunicorn)
   - `playto-worker` (Django-Q cluster + scheduler)
   - `playto-frontend` (Vite-built static site)
3. After first web deploy, set the frontend service's `VITE_API_BASE_URL` to the web service URL (e.g. `https://playto-web.onrender.com`) and redeploy frontend.
4. The web build command runs migrations, registers schedules, and seeds 3 merchants. All idempotent — re-running does nothing if data already exists.

---

<details>
<summary>Manual local dev (without Docker) — for working on the code</summary>

If you're iterating on the backend or frontend and want hot-reload outside Docker:

```bash
# Postgres on a non-conflicting host port
docker run -d --name playto-pg \
  -e POSTGRES_USER=playto -e POSTGRES_PASSWORD=playto -e POSTGRES_DB=playto \
  -p 5433:5432 postgres:16-alpine

# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py seed --reset
python manage.py setup_schedules

# In two more terminals
python manage.py runserver 8000
python manage.py qcluster

# Frontend (third terminal)
cd frontend
npm install
npm run dev
```

</details>
