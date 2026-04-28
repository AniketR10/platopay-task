"""
Money-moving operations.

Every function that changes the ledger or a payout's status holds a
`SELECT ... FOR UPDATE` on the merchant row for the duration of the change.
The merchant row is the per-merchant balance mutex: any two operations that
could affect a merchant's balance contend on it, so a check-then-deduct
sequence is safe even under concurrent requests.
"""
from __future__ import annotations

from typing import TypedDict

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .exceptions import IllegalStateTransition, InsufficientFunds
from .models import BankAccount, LedgerEntry, Merchant, Payout


class Balance(TypedDict):
    available_paise: int
    held_paise: int
    total_credited_paise: int
    total_debited_paise: int


def get_balance(merchant_id) -> Balance:
    """
    Pure database aggregation — no Python-side arithmetic on fetched rows.

    available = SUM(CREDIT) - SUM(DEBIT)        # spendable right now
    held      = SUM(DEBIT WHERE status=HOLD)    # locked by in-process payouts
    """
    aggregates = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
        total_credit=Coalesce(
            Sum("amount_paise", filter=Q(entry_type=LedgerEntry.EntryType.CREDIT)),
            0,
        ),
        total_debit=Coalesce(
            Sum("amount_paise", filter=Q(entry_type=LedgerEntry.EntryType.DEBIT)),
            0,
        ),
        held=Coalesce(
            Sum(
                "amount_paise",
                filter=Q(entry_type=LedgerEntry.EntryType.DEBIT)
                & Q(status=LedgerEntry.Status.HOLD),
            ),
            0,
        ),
    )
    available = aggregates["total_credit"] - aggregates["total_debit"]
    return {
        "available_paise": available,
        "held_paise": aggregates["held"],
        "total_credited_paise": aggregates["total_credit"],
        "total_debited_paise": aggregates["total_debit"],
    }


@transaction.atomic
def create_payout(*, merchant_id, bank_account_id, amount_paise: int) -> Payout:
    """
    Create a pending payout and a matching HOLD debit, atomically.

    Concurrency: SELECT FOR UPDATE on the merchant row blocks any other transaction
    that does the same. Two concurrent 60-rupee requests against a 100-rupee balance
    therefore serialize: one gets through, the other reads the post-hold balance
    (40) and is rejected with InsufficientFunds.
    """
    if amount_paise <= 0:
        raise InsufficientFunds("Amount must be positive.")

    # Lock the merchant row first; everything balance-touching after this point
    # is serialized for this merchant.
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)

    bank_account = BankAccount.objects.get(pk=bank_account_id, merchant_id=merchant.pk, is_active=True)

    balance = get_balance(merchant.pk)
    if amount_paise > balance["available_paise"]:
        raise InsufficientFunds(
            f"Insufficient funds: requested {amount_paise} paise, "
            f"available {balance['available_paise']} paise."
        )

    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        status=Payout.Status.PENDING,
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        entry_type=LedgerEntry.EntryType.DEBIT,
        status=LedgerEntry.Status.HOLD,
        amount_paise=amount_paise,
        payout=payout,
        description=f"Payout hold {payout.id}",
    )
    return payout


def _transition(payout_locked: Payout, *, to_status: str) -> None:
    """In-place state-machine guard. Caller must already hold the lock."""
    allowed = Payout.ALLOWED_TRANSITIONS.get(payout_locked.status, set())
    if to_status not in allowed:
        raise IllegalStateTransition(
            f"Payout {payout_locked.id}: illegal transition "
            f"{payout_locked.status} -> {to_status} "
            f"(allowed from {payout_locked.status}: {sorted(allowed)})"
        )
    payout_locked.status = to_status


@transaction.atomic
def begin_processing(payout_id) -> Payout:
    """
    PENDING -> PROCESSING (or PROCESSING -> PROCESSING for a retry).

    Idempotent on PROCESSING: a worker that picks up a payout already in
    PROCESSING just bumps `attempts` — useful for the stuck-payout retry path.
    """
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    if payout.status in Payout.TERMINAL_STATUSES:
        raise IllegalStateTransition(
            f"Payout {payout.id} is already terminal ({payout.status})."
        )
    _transition(payout, to_status=Payout.Status.PROCESSING)
    payout.attempts += 1
    payout.last_attempted_at = timezone.now()
    payout.save(update_fields=["status", "attempts", "last_attempted_at", "updated_at"])
    return payout


@transaction.atomic
def complete_payout(payout_id) -> Payout:
    """PROCESSING -> COMPLETED. Settles the HOLD debit (HOLD -> POSTED)."""
    # Lock merchant first to keep lock acquisition order consistent with create_payout
    # and avoid deadlocks.
    payout = Payout.objects.select_related("merchant").get(pk=payout_id)
    Merchant.objects.select_for_update().get(pk=payout.merchant_id)
    payout = Payout.objects.select_for_update().get(pk=payout_id)

    _transition(payout, to_status=Payout.Status.COMPLETED)
    payout.failure_reason = ""
    payout.save(update_fields=["status", "failure_reason", "updated_at"])

    LedgerEntry.objects.filter(
        payout=payout, status=LedgerEntry.Status.HOLD
    ).update(status=LedgerEntry.Status.POSTED, updated_at=timezone.now())
    return payout


@transaction.atomic
def fail_payout(payout_id, *, reason: str) -> Payout:
    """
    PROCESSING -> FAILED. Settles the HOLD debit AND inserts a reversing CREDIT
    of the same amount. Net effect on the available balance is zero — the
    held funds become available again — and the ledger is balanced.
    """
    payout = Payout.objects.select_related("merchant").get(pk=payout_id)
    Merchant.objects.select_for_update().get(pk=payout.merchant_id)
    payout = Payout.objects.select_for_update().get(pk=payout_id)

    _transition(payout, to_status=Payout.Status.FAILED)
    payout.failure_reason = reason
    payout.save(update_fields=["status", "failure_reason", "updated_at"])

    held_debits = list(
        LedgerEntry.objects.filter(payout=payout, status=LedgerEntry.Status.HOLD)
    )
    for debit in held_debits:
        debit.status = LedgerEntry.Status.POSTED
        debit.save(update_fields=["status", "updated_at"])
        LedgerEntry.objects.create(
            merchant_id=payout.merchant_id,
            entry_type=LedgerEntry.EntryType.CREDIT,
            status=LedgerEntry.Status.POSTED,
            amount_paise=debit.amount_paise,
            payout=payout,
            description=f"Refund for failed payout {payout.id}",
        )
    return payout


@transaction.atomic
def credit_merchant(*, merchant_id, amount_paise: int, description: str = "") -> LedgerEntry:
    """Helper used by the seed script to simulate customer payments."""
    if amount_paise <= 0:
        raise ValueError("Credit amount must be positive.")
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)
    return LedgerEntry.objects.create(
        merchant=merchant,
        entry_type=LedgerEntry.EntryType.CREDIT,
        status=LedgerEntry.Status.POSTED,
        amount_paise=amount_paise,
        description=description or "Customer payment",
    )
