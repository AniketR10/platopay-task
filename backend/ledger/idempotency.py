"""
Idempotency-Key handling for the payout creation endpoint.

Contract
--------
- Header `Idempotency-Key` must be a non-empty string (we accept any client-side
  format; UUID is recommended).
- Keys are scoped per merchant.
- A second request with the same (merchant, key) returns the cached response
  byte-for-byte if the first request committed.
- A second request that arrives while the first is still in flight blocks on
  the unique constraint at INSERT time, then reads the committed response.
- A second request with the same key but a different request body returns 409.
- Keys older than IDEMPOTENCY_TTL_HOURS are treated as if they don't exist.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyKey


def _to_json_safe(obj: Any) -> Any:
    """Coerce DRF ReturnDicts / UUIDs / datetimes to plain JSON-compatible types."""
    return json.loads(json.dumps(obj, cls=DjangoJSONEncoder))


def fingerprint_request(merchant_id, body: dict[str, Any]) -> str:
    """Stable hash of the request, used to detect key reuse with a different body."""
    payload = json.dumps(
        {"merchant_id": str(merchant_id), "body": body},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class IdempotencyResult:
    """One of: replay (return cached), conflict (different body), fresh (do the work)."""
    kind: str  # "replay" | "conflict" | "fresh"
    cached_status: int | None = None
    cached_body: Any = None
    record: IdempotencyKey | None = None  # populated when kind == "fresh"


def acquire_idempotency_slot(
    *, merchant_id, key: str, fingerprint: str
) -> IdempotencyResult:
    """
    Try to claim the (merchant, key) slot.

    The unique constraint on (merchant_id, key) is what serializes us against
    a concurrent attempt with the same key: the loser's INSERT blocks until the
    winner's transaction commits, then raises IntegrityError. The loser then
    reads the winner's response and returns it.

    Must be called inside an outer `transaction.atomic()` so that both this
    INSERT and the subsequent business work commit together. We use a savepoint
    around the INSERT so we can recover from IntegrityError without aborting
    the outer transaction.
    """
    now = timezone.now()
    ttl = timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS)

    # Treat expired rows as gone. Doing this first means a subsequent INSERT
    # for the same key won't conflict with a stale row.
    IdempotencyKey.objects.filter(
        merchant_id=merchant_id, key=key, expires_at__lte=now
    ).delete()

    try:
        with transaction.atomic():  # savepoint
            record = IdempotencyKey.objects.create(
                merchant_id=merchant_id,
                key=key,
                request_fingerprint=fingerprint,
                expires_at=now + ttl,
            )
        return IdempotencyResult(kind="fresh", record=record)
    except IntegrityError:
        # Another transaction owns the key. It has already committed (otherwise
        # our INSERT would still be blocked, not failing).
        existing = IdempotencyKey.objects.get(merchant_id=merchant_id, key=key)

        if existing.request_fingerprint != fingerprint:
            return IdempotencyResult(kind="conflict")

        if existing.response_status is None:
            # Owner committed without filling in the response. Two ways this
            # happens: (1) the worker process died between INSERT and final
            # UPDATE — extremely rare given they're in the same tx; (2) the
            # owner is mid-transaction and we somehow saw it. We surface 409 so
            # the client retries.
            return IdempotencyResult(kind="conflict")

        return IdempotencyResult(
            kind="replay",
            cached_status=existing.response_status,
            cached_body=existing.response_body,
        )


def store_response(record: IdempotencyKey, *, status: int, body: Any, payout=None) -> None:
    """Save the final response onto the slot we own. Caller is in the same outer atomic."""
    record.response_status = status
    record.response_body = _to_json_safe(body)
    if payout is not None:
        record.payout = payout
    record.save(update_fields=["response_status", "response_body", "payout"])
