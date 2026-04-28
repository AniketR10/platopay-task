from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .exceptions import InsufficientFunds
from .idempotency import acquire_idempotency_slot, fingerprint_request, store_response
from .models import BankAccount, LedgerEntry, Merchant, Payout
from .serializers import (
    BankAccountSerializer,
    CreatePayoutRequestSerializer,
    LedgerEntrySerializer,
    MerchantSerializer,
    PayoutSerializer,
)
from .services import create_payout, get_balance


@api_view(["GET"])
def list_merchants(request):
    return Response(MerchantSerializer(Merchant.objects.all(), many=True).data)


@api_view(["GET"])
def merchant_detail(request, merchant_id):
    merchant = get_object_or_404(Merchant, pk=merchant_id)
    balance = get_balance(merchant.pk)
    return Response({
        "merchant": MerchantSerializer(merchant).data,
        "balance": balance,
        "bank_accounts": BankAccountSerializer(merchant.bank_accounts.all(), many=True).data,
    })


@api_view(["GET"])
def merchant_ledger(request, merchant_id):
    get_object_or_404(Merchant, pk=merchant_id)
    entries = LedgerEntry.objects.filter(merchant_id=merchant_id).order_by("-created_at")[:100]
    return Response(LedgerEntrySerializer(entries, many=True).data)


@api_view(["GET"])
def merchant_payouts(request, merchant_id):
    get_object_or_404(Merchant, pk=merchant_id)
    payouts = Payout.objects.filter(merchant_id=merchant_id).order_by("-created_at")[:100]
    return Response(PayoutSerializer(payouts, many=True).data)


@api_view(["POST"])
def create_payout_view(request):
    """
    POST /api/v1/payouts

    Headers
        Idempotency-Key: <client-supplied string, scoped per merchant>
        X-Merchant-Id:   <merchant uuid; stand-in for real auth>

    Body
        { "amount_paise": int, "bank_account_id": uuid }
    """
    idem_key = request.headers.get("Idempotency-Key", "").strip()
    if not idem_key:
        return Response(
            {"error": "missing_idempotency_key", "message": "Idempotency-Key header is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    merchant_id = request.headers.get("X-Merchant-Id", "").strip()
    if not merchant_id:
        return Response(
            {"error": "missing_merchant", "message": "X-Merchant-Id header is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    merchant = get_object_or_404(Merchant, pk=merchant_id)

    req = CreatePayoutRequestSerializer(data=request.data)
    req.is_valid(raise_exception=True)
    body = req.validated_data
    fingerprint = fingerprint_request(merchant.pk, {
        "amount_paise": body["amount_paise"],
        "bank_account_id": str(body["bank_account_id"]),
    })

    # Whole-request transaction so the idempotency record commits atomically
    # with the payout it idempotizes (or rolls back together on any failure).
    with transaction.atomic():
        slot = acquire_idempotency_slot(
            merchant_id=merchant.pk, key=idem_key, fingerprint=fingerprint
        )

        if slot.kind == "replay":
            return Response(slot.cached_body, status=slot.cached_status)
        if slot.kind == "conflict":
            return Response(
                {
                    "error": "idempotency_conflict",
                    "message": "Idempotency-Key reused with a different request "
                               "or the original request did not complete; retry with a new key.",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # We own the key. Try to create the payout.
        try:
            payout = create_payout(
                merchant_id=merchant.pk,
                bank_account_id=body["bank_account_id"],
                amount_paise=body["amount_paise"],
            )
        except BankAccount.DoesNotExist:
            response_body = {"error": "invalid_bank_account"}
            response_status = status.HTTP_400_BAD_REQUEST
            store_response(slot.record, status=response_status, body=response_body)
            return Response(response_body, status=response_status)
        except InsufficientFunds as exc:
            response_body = {"error": "insufficient_funds", "message": str(exc)}
            response_status = status.HTTP_422_UNPROCESSABLE_ENTITY
            store_response(slot.record, status=response_status, body=response_body)
            return Response(response_body, status=response_status)

        response_body = PayoutSerializer(payout).data
        response_status = status.HTTP_201_CREATED
        store_response(slot.record, status=response_status, body=response_body, payout=payout)

        # Enqueue the worker. async_task writes to the django_q ORM table inside
        # the same transaction, so the worker only sees the task after we commit;
        # if we roll back, the task vanishes too.
        from django_q.tasks import async_task
        async_task("ledger.tasks.process_payout", str(payout.id))

    return Response(response_body, status=response_status)


@api_view(["GET"])
def payout_detail(request, payout_id):
    payout = get_object_or_404(Payout, pk=payout_id)
    return Response(PayoutSerializer(payout).data)
