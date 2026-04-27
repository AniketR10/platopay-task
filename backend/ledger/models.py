"""
Data model for the Playto payout engine.

Money modeling
--------------
Every money movement is a row in `LedgerEntry`. The ledger is append-only;
balances are derived, never stored. A merchant's available balance is exactly
`SUM(CREDIT.amount) - SUM(DEBIT.amount)` over all entries — by construction,
not by maintenance, so there is no second source of truth.

A payout reserves funds by inserting a DEBIT with status=HOLD. When the payout
reaches a terminal state, the debit's status flips to POSTED. If it failed, an
additional reversing CREDIT is inserted referencing the same payout — this is
why the `SUM credits - SUM debits` invariant holds for every state of every
payout, including failures.
"""
from __future__ import annotations

import uuid
from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint


class Merchant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.name


class BankAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="bank_accounts")
    account_holder_name = models.CharField(max_length=200)
    account_number_masked = models.CharField(max_length=32)
    ifsc = models.CharField(max_length=11)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.account_holder_name} ({self.account_number_masked})"


class LedgerEntry(models.Model):
    """Append-only money movement record. Only updated for HOLD->POSTED on debits."""

    class EntryType(models.TextChoices):
        CREDIT = "CREDIT", "Credit"
        DEBIT = "DEBIT", "Debit"

    class Status(models.TextChoices):
        # CREDIT entries are always POSTED. DEBIT entries start HOLD and become POSTED
        # when the related payout reaches a terminal state (success OR failure).
        HOLD = "HOLD", "Hold"
        POSTED = "POSTED", "Posted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="ledger_entries")
    entry_type = models.CharField(max_length=8, choices=EntryType.choices)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.POSTED)
    amount_paise = models.BigIntegerField()
    payout = models.ForeignKey(
        "Payout",
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        null=True,
        blank=True,
    )
    description = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["merchant", "created_at"]),
            models.Index(fields=["payout"]),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(amount_paise__gt=0),
                name="ledger_amount_positive",
            ),
            CheckConstraint(
                condition=~(Q(entry_type="CREDIT") & Q(status="HOLD")),
                name="ledger_credit_never_hold", 
            ),
        ]

    def __str__(self) -> str:
        sign = "+" if self.entry_type == self.EntryType.CREDIT else "-"
        return f"{sign}{self.amount_paise} ({self.status}) for {self.merchant_id}"


class Payout(models.Model):
    """One payout request. Strict state machine; see ledger.services.transition_payout."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    TERMINAL_STATUSES = {Status.COMPLETED, Status.FAILED}

    # Allowed forward transitions. Anything not in this set is rejected.
    ALLOWED_TRANSITIONS = {
        Status.PENDING: {Status.PROCESSING},
        Status.PROCESSING: {Status.PROCESSING, Status.COMPLETED, Status.FAILED},
        Status.COMPLETED: set(),
        Status.FAILED: set(),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="payouts")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="payouts")
    amount_paise = models.BigIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.IntegerField(default=0)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["merchant", "-created_at"]),
            models.Index(fields=["status", "last_attempted_at"]),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(amount_paise__gt=0),
                name="payout_amount_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"Payout {self.id} {self.amount_paise} paise ({self.status})"


class IdempotencyKey(models.Model):
    """
    Per-merchant cache of API responses keyed by the client-supplied UUID.

    The cache is filled atomically with the request it idempotizes:
    a row is INSERTed at the top of the request transaction with response_status
    NULL, and UPDATEd with the response just before COMMIT. A concurrent second
    request with the same key blocks on the unique constraint until the first
    transaction commits or rolls back, then either returns the stored response
    (if first committed) or proceeds itself (if first rolled back).
    """
    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="idempotency_keys")
    key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    payout = models.ForeignKey(
        Payout, on_delete=models.SET_NULL, null=True, blank=True, related_name="idempotency_keys"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            UniqueConstraint(fields=["merchant", "key"], name="idem_key_unique_per_merchant"),
        ]
        indexes = [
            models.Index(fields=["expires_at"]),
        ]
