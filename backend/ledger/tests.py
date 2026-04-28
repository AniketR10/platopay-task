"""
Tests for the parts of the system most likely to break under load:
  - Concurrency: two simultaneous payouts cannot overdraw a balance.
  - Idempotency: same key returns the same response, conflicting key body is rejected.
  - State machine: terminal payout states are not transitionable.

The concurrency test must hit a real Postgres (sqlite has no row-level locks
that work across threads in the way we rely on). It uses
TransactionTestCase + threads + a barrier so both threads attempt the deduct
at as close to the same instant as possible.
"""
from __future__ import annotations

import threading
import uuid

from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse  # noqa: F401  (kept for future reverse() use)

from ledger.exceptions import IllegalStateTransition, InsufficientFunds
from ledger.models import BankAccount, IdempotencyKey, LedgerEntry, Merchant, Payout
from ledger.services import (
    begin_processing,
    complete_payout,
    create_payout,
    credit_merchant,
    fail_payout,
    get_balance,
)


def _make_merchant(name: str = "Test", credit_paise: int = 10_000) -> tuple[Merchant, BankAccount]:
    m = Merchant.objects.create(name=name)
    ba = BankAccount.objects.create(
        merchant=m,
        account_holder_name="Holder",
        account_number_masked="****0000",
        ifsc="HDFC0000001",
    )
    if credit_paise:
        credit_merchant(merchant_id=m.id, amount_paise=credit_paise, description="seed")
    return m, ba


class StateMachineTests(TestCase):
    """The check that question 4 of EXPLAINER points to."""

    def test_terminal_states_cannot_transition_anywhere(self):
        m, ba = _make_merchant(credit_paise=10_000)
        p = create_payout(merchant_id=m.id, bank_account_id=ba.id, amount_paise=2_000)
        begin_processing(p.id)
        complete_payout(p.id)

        # COMPLETED -> FAILED is illegal.
        with self.assertRaises(IllegalStateTransition):
            fail_payout(p.id, reason="should not work")
        # COMPLETED -> any other state via begin_processing is also illegal.
        with self.assertRaises(IllegalStateTransition):
            begin_processing(p.id)

        # FAILED can't be moved either.
        p2 = create_payout(merchant_id=m.id, bank_account_id=ba.id, amount_paise=2_000)
        begin_processing(p2.id)
        fail_payout(p2.id, reason="simulated")
        with self.assertRaises(IllegalStateTransition):
            complete_payout(p2.id)


class FailureRefundTests(TestCase):
    """The reversing-credit pattern: ledger sums must equal the displayed balance."""

    def test_failed_payout_restores_available_balance_via_reversing_credit(self):
        m, ba = _make_merchant(credit_paise=10_000)
        before = get_balance(m.id)["available_paise"]

        p = create_payout(merchant_id=m.id, bank_account_id=ba.id, amount_paise=4_000)
        self.assertEqual(get_balance(m.id)["available_paise"], before - 4_000)
        self.assertEqual(get_balance(m.id)["held_paise"], 4_000)

        begin_processing(p.id)
        fail_payout(p.id, reason="bank rejected")

        # Available restored, nothing held, and the literal SUM(C)-SUM(D) holds.
        bal = get_balance(m.id)
        self.assertEqual(bal["available_paise"], before)
        self.assertEqual(bal["held_paise"], 0)
        self.assertEqual(
            bal["available_paise"],
            bal["total_credited_paise"] - bal["total_debited_paise"],
        )


class IdempotencyTests(TestCase):
    """Spec asks for idempotent payout creation; this verifies the API contract."""

    def setUp(self):
        self.m, self.ba = _make_merchant(credit_paise=50_000)
        self.headers = {
            "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4()),
            "HTTP_X_MERCHANT_ID": str(self.m.id),
        }

    def _post(self, body, **header_overrides):
        headers = {**self.headers, **header_overrides}
        return self.client.post(
            "/api/v1/payouts",
            data=body,
            content_type="application/json",
            **headers,
        )

    def test_same_key_same_body_returns_cached_response(self):
        body = {"amount_paise": 1500, "bank_account_id": str(self.ba.id)}
        r1 = self._post(body)
        r2 = self._post(body)

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        # Same payout id — no duplicate created.
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(Payout.objects.filter(merchant=self.m).count(), 1)
        # Idempotency row stored exactly once.
        self.assertEqual(IdempotencyKey.objects.filter(merchant=self.m).count(), 1)

    def test_same_key_different_body_returns_409(self):
        r1 = self._post({"amount_paise": 1500, "bank_account_id": str(self.ba.id)})
        self.assertEqual(r1.status_code, 201)
        r2 = self._post({"amount_paise": 9999, "bank_account_id": str(self.ba.id)})
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(Payout.objects.filter(merchant=self.m).count(), 1)

    def test_missing_idempotency_key_rejected(self):
        r = self.client.post(
            "/api/v1/payouts",
            data={"amount_paise": 100, "bank_account_id": str(self.ba.id)},
            content_type="application/json",
            HTTP_X_MERCHANT_ID=str(self.m.id),
        )
        self.assertEqual(r.status_code, 400)


@override_settings(DEBUG=False)  # reduce noise; real DB tx behavior is identical
class ConcurrencyTests(TransactionTestCase):
    """
    Two simultaneous over-spends against a 100-rupee balance: exactly one wins.

    TransactionTestCase (not TestCase) is required because the test must commit
    real transactions for `SELECT FOR UPDATE` to actually contend across threads.
    """

    reset_sequences = True

    def test_two_concurrent_payouts_only_one_succeeds(self):
        # 100 rupees = 10_000 paise; two 60-rupee requests = 6_000 paise each.
        m, ba = _make_merchant(name="Race", credit_paise=10_000)
        merchant_id, bank_account_id = m.id, ba.id

        N = 2
        barrier = threading.Barrier(N)
        results: list[object] = []
        results_lock = threading.Lock()

        def worker():
            # Each thread gets its own connection (closed at end), and we sync
            # at the barrier so both threads call create_payout simultaneously.
            try:
                barrier.wait(timeout=5)
                payout = create_payout(
                    merchant_id=merchant_id,
                    bank_account_id=bank_account_id,
                    amount_paise=6_000,
                )
                with results_lock:
                    results.append(("ok", payout.id))
            except Exception as e:  # noqa: BLE001  (we want to capture all failures)
                with results_lock:
                    results.append(("err", type(e).__name__, str(e)))
            finally:
                # Worker threads must explicitly close their per-thread DB
                # connection — close_old_connections() only kicks in past
                # CONN_MAX_AGE. Without this, the test DB cannot be dropped on teardown.
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Reset connection so we see committed state.
        connection.close()

        successes = [r for r in results if r[0] == "ok"]
        failures = [r for r in results if r[0] == "err"]

        self.assertEqual(len(successes), 1, f"expected exactly 1 success, got {results}")
        self.assertEqual(len(failures), 1, f"expected exactly 1 failure, got {results}")
        self.assertEqual(failures[0][1], "InsufficientFunds")

        # Database should reflect: one Payout, one HOLD debit, balance held=6_000.
        self.assertEqual(Payout.objects.filter(merchant_id=merchant_id).count(), 1)
        bal = get_balance(merchant_id)
        self.assertEqual(bal["held_paise"], 6_000)
        self.assertEqual(bal["available_paise"], 4_000)

    def test_high_concurrency_all_or_nothing_invariant(self):
        """Stress: 10 simultaneous requests of 30 paise against 100 paise balance.

        Up to 3 must succeed (90 paise total), at least 7 must fail.
        Balance must never go negative.
        """
        m, ba = _make_merchant(name="Stress", credit_paise=100)
        merchant_id, bank_account_id = m.id, ba.id

        N = 10
        per_request_paise = 30
        barrier = threading.Barrier(N)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker():
            try:
                barrier.wait(timeout=5)
                create_payout(
                    merchant_id=merchant_id,
                    bank_account_id=bank_account_id,
                    amount_paise=per_request_paise,
                )
                with results_lock:
                    results.append(True)
            except InsufficientFunds:
                with results_lock:
                    results.append(False)
            finally:
                # Worker threads must explicitly close their per-thread DB
                # connection — close_old_connections() only kicks in past
                # CONN_MAX_AGE. Without this, the test DB cannot be dropped on teardown.
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        connection.close()

        successes = sum(1 for r in results if r)
        failures = sum(1 for r in results if not r)
        self.assertEqual(successes + failures, N)
        # 100 / 30 = 3 successes max
        self.assertLessEqual(successes, 3)
        self.assertGreaterEqual(failures, N - 3)

        bal = get_balance(merchant_id)
        self.assertGreaterEqual(bal["available_paise"], 0, "balance went NEGATIVE")
        self.assertEqual(bal["held_paise"], successes * per_request_paise)
        # Invariant: literal SUM(C)-SUM(D) == available_paise
        self.assertEqual(
            bal["available_paise"],
            bal["total_credited_paise"] - bal["total_debited_paise"],
        )
