"""
Background tasks executed by the Django-Q cluster.

Two tasks:
- process_payout: handles one attempt at moving a payout to a terminal state.
  Simulates bank settlement with the configured success/fail/hang ratios.
- retry_stuck_payouts: scheduled (every ~10s); retries payouts that have been
  in PROCESSING longer than PAYOUT_STUCK_AFTER_SECONDS, with exponential
  backoff and a max attempt cap. The lifecycle is:

      PENDING --enqueue--> PROCESSING --[70%]--> COMPLETED
                                       --[20%]--> FAILED (refund)
                                       --[10%]--> hangs (no transition)

  The retry task is what unblocks the 10% hang case, and what enforces the
  3-attempt cap → FAILED + refund.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_q.tasks import async_task

from .exceptions import IllegalStateTransition
from .models import Payout
from .services import begin_processing, complete_payout, fail_payout

log = logging.getLogger(__name__)


def process_payout(payout_id: str) -> str:
    """
    One settlement attempt. Returns a short status string for django-q's task log.

    Outcomes (controlled by PAYOUT_SUCCESS_RATE / PAYOUT_FAIL_RATE / PAYOUT_HANG_RATE):
      - success → COMPLETED
      - fail    → FAILED with refund
      - hang    → leaves status=PROCESSING; retry_stuck_payouts handles it later
    """
    try:
        payout = begin_processing(payout_id)
    except IllegalStateTransition as exc:
        # Already terminal or otherwise not eligible — nothing to do.
        log.info("process_payout: skip %s (%s)", payout_id, exc)
        return f"skipped: {exc}"

    # Roll the dice. Sum should be 1.0; we don't normalize so misconfiguration is visible.
    r = random.random()
    if r < settings.PAYOUT_SUCCESS_RATE:
        complete_payout(payout.id)
        return "completed"

    if r < settings.PAYOUT_SUCCESS_RATE + settings.PAYOUT_FAIL_RATE:
        fail_payout(payout.id, reason="Simulated bank settlement failure.")
        return "failed"

    # The "hang" case: just sleep and don't transition. The retry task will
    # observe the stale last_attempted_at and re-enqueue this payout.
    log.info("process_payout: %s simulated hang — leaving in PROCESSING", payout_id)
    time.sleep(0.5)  # brief sleep so the task slot frees up; not required for correctness
    return "hung"


def retry_stuck_payouts() -> str:
    """
    Find PROCESSING payouts older than PAYOUT_STUCK_AFTER_SECONDS and either:
      - re-enqueue them (with exponential backoff respect — see below), or
      - fail+refund them if they've hit PAYOUT_MAX_ATTEMPTS.

    Exponential backoff: a payout that has been retried `n` times must wait
    PAYOUT_STUCK_AFTER_SECONDS * (2^(n-1)) seconds since its last_attempted_at
    before the next retry. So with stuck=30s and max=3:
        attempt 1: enqueued immediately
        attempt 2: ~30s after attempt 1
        attempt 3: ~60s after attempt 2
        attempt 4: not allowed → FAILED+refund
    """
    now = timezone.now()
    stuck_seconds = settings.PAYOUT_STUCK_AFTER_SECONDS
    max_attempts = settings.PAYOUT_MAX_ATTEMPTS
    actions: list[str] = []

    candidates = Payout.objects.filter(
        status=Payout.Status.PROCESSING,
        last_attempted_at__lte=now - timedelta(seconds=stuck_seconds),
    ).order_by("last_attempted_at")[:50]

    for payout in candidates:
        wait_seconds = stuck_seconds * (2 ** max(0, payout.attempts - 1))
        if payout.last_attempted_at and (now - payout.last_attempted_at).total_seconds() < wait_seconds:
            continue  # backoff window not yet elapsed for this attempt count

        if payout.attempts >= max_attempts:
            try:
                fail_payout(
                    payout.id,
                    reason=f"Exceeded {max_attempts} settlement attempts; gave up.",
                )
                actions.append(f"{payout.id}:gave_up")
            except IllegalStateTransition:
                pass  # raced with another worker that already terminated it
            continue

        async_task("ledger.tasks.process_payout", str(payout.id))
        actions.append(f"{payout.id}:requeued")

    return ", ".join(actions) if actions else "no stuck payouts"


def cleanup_expired_idempotency_keys() -> int:
    """Delete idempotency rows whose TTL has passed. Run periodically."""
    from .models import IdempotencyKey
    deleted, _ = IdempotencyKey.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted
