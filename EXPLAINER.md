# EXPLAINER

Five questions, five direct answers.

---

## 1. The Ledger

**Balance query** ([`services.py:30-58`](backend/ledger/services.py#L30-L58)):

```python
aggregates = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
    total_credit=Coalesce(Sum("amount_paise", filter=Q(entry_type="CREDIT")), 0),
    total_debit =Coalesce(Sum("amount_paise", filter=Q(entry_type="DEBIT")),  0),
    held        =Coalesce(Sum("amount_paise",
                   filter=Q(entry_type="DEBIT") & Q(status="HOLD")), 0),
)
available = aggregates["total_credit"] - aggregates["total_debit"]
```

One SQL statement, three filtered `SUM()`s, server-side. No Python arithmetic over fetched rows. Amounts are `BigIntegerField` paise — never floats, never decimals.

**Why this shape**

- **No stored `balance` column.** A second source of truth can drift; a derived balance can't. The spec's invariant (*"sum of credits minus debits must always equal the displayed balance"*) is true **by construction**, not by maintenance.
- **Reversing credits for failed payouts.** When a payout fails, instead of deleting or "voiding" the debit, we mark it POSTED *and* insert a new CREDIT for the refund. Result: the literal `SUM(C) − SUM(D)` always equals the available balance, no special-case filters needed.
- **Two debit statuses (`HOLD`, `POSTED`).** Both count against the balance equally. `HOLD` only exists so the dashboard can show held funds separately. `HOLD → POSTED` is the only mutable transition in the entire ledger.

**Trace**

| Action | Ledger row | Available | Held |
|---|---|---|---|
| Customer pays ₹100 | `CREDIT POSTED 10000` | 10000 | 0 |
| Payout ₹60 requested | `DEBIT HOLD 6000` | 4000 | 6000 |
| Payout completes | (debit flips to POSTED) | 4000 | 0 |
| ₹20 payout fails | `DEBIT POSTED 2000` + `CREDIT POSTED 2000` (refund) | 4000 | 0 |

`SUM(C) = 12000, SUM(D) = 8000, available = 4000`. ✓

---

## 2. The Lock

**The exact code** ([`services.py:62-101`](backend/ledger/services.py#L62-L101)):

```python
@transaction.atomic
def create_payout(*, merchant_id, bank_account_id, amount_paise: int) -> Payout:
    # Lock the merchant row — every balance-changing op for this merchant
    # contends on this single row.
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)

    bank_account = BankAccount.objects.get(pk=bank_account_id, merchant_id=merchant.pk, is_active=True)

    balance = get_balance(merchant.pk)
    if amount_paise > balance["available_paise"]:
        raise InsufficientFunds(...)

    payout = Payout.objects.create(merchant=merchant, ..., status="PENDING")
    LedgerEntry.objects.create(merchant=merchant, entry_type="DEBIT",
                               status="HOLD", amount_paise=amount_paise, payout=payout)
    return payout
```

**The primitive: PostgreSQL row-level write locks (`SELECT ... FOR UPDATE`).**

The merchant row is the per-merchant **balance mutex**. Two concurrent requests for the same merchant queue on this lock: the first runs to commit, the second blocks, and when it unblocks it sees the held debit from the first → balance recomputed → rejected with `InsufficientFunds`.

**Why the merchant row, not the ledger?** `SELECT FOR UPDATE` only locks rows it returns. Locking ledger rows wouldn't help — two concurrent requests would lock the same existing rows and neither would block the other's *new* INSERT. We need a single anchor row that all balance-changing ops contend on. The merchant row fits: small, indexed, exactly one per merchant.

**Verified by** [`tests.py`](backend/ledger/tests.py): `test_two_concurrent_payouts_only_one_succeeds` (real threads, real Postgres, `TransactionTestCase`) and `test_high_concurrency_all_or_nothing_invariant` (10 threads — balance never goes negative).

---

## 3. The Idempotency

**How it knows it's seen a key before:** a unique constraint on `(merchant_id, key)` in the `IdempotencyKey` table. Postgres enforces it, not Python.

**Acquisition flow** ([`idempotency.py:48-92`](backend/ledger/idempotency.py#L48-L92)):

```python
# Treat expired rows as gone, so a fresh INSERT can proceed.
IdempotencyKey.objects.filter(
    merchant_id=merchant_id, key=key, expires_at__lte=now
).delete()

try:
    with transaction.atomic():  # savepoint
        record = IdempotencyKey.objects.create(...)
    return IdempotencyResult(kind="fresh", record=record)
except IntegrityError:
    existing = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)
    if existing.request_fingerprint != fingerprint:
        return IdempotencyResult(kind="conflict")        # → 409
    return IdempotencyResult(kind="replay",
                             cached_status=existing.response_status,
                             cached_body=existing.response_body)
```

The view wraps the entire request in `transaction.atomic()`, so the idempotency row, the payout, the ledger entries, and the worker enqueue all commit together (or roll back together).

**What if the first request is in flight when the second arrives?**

The second's INSERT **blocks at the unique constraint** until the first transaction commits or rolls back — Postgres handles the wait, no polling, no busy-loop.

- First commits → second's INSERT raises `IntegrityError` → second reads the stored response and returns it as a replay.
- First rolls back → second's INSERT succeeds → second becomes the new "first" and does the work.

A `request_fingerprint` (sha256 of body) is stored alongside the key. Same key + different body → 409. Per-merchant scoping via the unique constraint; 24h TTL via `expires_at`; an hourly cleanup task bounds table size.

---

## 4. The State Machine

**Where failed-to-completed is blocked** ([`services.py:107-116`](backend/ledger/services.py#L107-L116)):

```python
def _transition(payout_locked: Payout, *, to_status: str) -> None:
    allowed = Payout.ALLOWED_TRANSITIONS.get(payout_locked.status, set())
    if to_status not in allowed:
        raise IllegalStateTransition(
            f"Payout {payout_locked.id}: illegal transition "
            f"{payout_locked.status} -> {to_status}"
        )
    payout_locked.status = to_status
```

The map ([`models.py:111-116`](backend/ledger/models.py#L111-L116)):

```python
ALLOWED_TRANSITIONS = {
    Status.PENDING:    {Status.PROCESSING},
    Status.PROCESSING: {Status.PROCESSING, Status.COMPLETED, Status.FAILED},
    Status.COMPLETED:  set(),  # terminal
    Status.FAILED:     set(),  # terminal
}
```

Terminal states have empty allowed-sets, so any transition out of them raises. The check runs **inside the `SELECT FOR UPDATE` lock on the payout row**, so two concurrent attempts to transition the same payout queue, and only the first reads a non-terminal status.

**Atomicity of failure + refund.** `fail_payout` flips the state, settles the HOLD debit, and inserts the reversing CREDIT inside one `@transaction.atomic`. There is never a moment where a payout is `FAILED` but funds aren't returned, or a refund credit exists without a matching failed payout.

**Verified by** `StateMachineTests.test_terminal_states_cannot_transition_anywhere`.

---

## 5. The AI Audit

Three real bugs from this build. All caught at runtime, not in code review.

### A. Locking the wrong row

**AI's first suggestion:**
```python
LedgerEntry.objects.filter(merchant_id=merchant_id).select_for_update()
balance = compute_balance(...)
if balance < amount: ...
```

**Why it's wrong:** `SELECT FOR UPDATE` only locks rows it *currently returns*. Two concurrent requests both see the same existing entries and lock that same set — but the *new* rows each is about to INSERT aren't part of the lock. Both pass the balance check, both insert, balance goes negative.

**Replaced with** ([`services.py:79`](backend/ledger/services.py#L79)): `Merchant.objects.select_for_update().get(pk=merchant_id)`. One anchor row that all ops contend on.

### B. `close_old_connections()` doesn't actually close them

**AI's suggestion in the concurrency test cleanup:**
```python
finally:
    close_old_connections()
```

**Why it's wrong:** `close_old_connections()` only closes connections older than `CONN_MAX_AGE`. We had `CONN_MAX_AGE=60`, so the helper was a silent no-op. Worker threads leaked connections, and the test database couldn't be dropped on teardown:
```
django.db.utils.OperationalError: database "test_playto" is being accessed by other users
```

**Replaced with** ([`tests.py`](backend/ledger/tests.py)): `connections.close_all()`. Caught from the failing teardown, not from reading the code.

### C. UUID into JSONField — the cross-layer assumption

**AI's first version of `store_response()`:**
```python
record.response_body = body  # body is DRF's ReturnDict containing UUIDs
record.save()
```

**Why it's wrong:** `JSONField` writes go through the stdlib `json.dumps()`, which can't serialize `UUID`. Result, mid-transaction:
```
TypeError: Object of type UUID is not JSON serializable
when serializing rest_framework.utils.serializer_helpers.ReturnDict item 'merchant'
```

The transaction rolled back. Worse second-order effect: the rollback also discarded the idempotency row, so the next request with the same key didn't replay — it 500'd again. The endpoint was completely broken for any successful create.

**Replaced with** ([`idempotency.py:23-25`](backend/ledger/idempotency.py#L23-L25)):
```python
def _to_json_safe(obj):
    return json.loads(json.dumps(obj, cls=DjangoJSONEncoder))
```

**Why this one is the worth confessing.** It would pass code review — `record.response_body = body` looks fine. It only fires on the happy path, so a unit test that mocks the serializer wouldn't catch it. I caught it from the Django error log running real curl tests. The general lesson: any time a value crosses from typed-Python (DRF, ORM) to untyped-storage (JSONField), the type assumptions on each side need to actually meet.
