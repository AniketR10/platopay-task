from django.urls import path

from . import views

urlpatterns = [
    path("merchants", views.list_merchants),
    path("merchants/<uuid:merchant_id>", views.merchant_detail),
    path("merchants/<uuid:merchant_id>/ledger", views.merchant_ledger),
    path("merchants/<uuid:merchant_id>/payouts", views.merchant_payouts),

    # Spec endpoint. Merchant context is supplied via the X-Merchant-Id header
    # (a stand-in for real auth in this take-home).
    path("payouts", views.create_payout_view),
    path("payouts/<uuid:payout_id>", views.payout_detail),
]
