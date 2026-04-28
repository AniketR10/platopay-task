from django.contrib import admin

from .models import BankAccount, IdempotencyKey, LedgerEntry, Merchant, Payout


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name", "id")


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "account_holder_name", "account_number_masked", "is_active")
    list_filter = ("is_active",)


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "entry_type", "status", "amount_paise", "payout", "created_at")
    list_filter = ("entry_type", "status")
    search_fields = ("merchant__name",)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "amount_paise", "status", "attempts", "last_attempted_at", "created_at")
    list_filter = ("status",)
    readonly_fields = ("attempts", "last_attempted_at", "created_at", "updated_at")


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "merchant", "key", "response_status", "payout", "created_at", "expires_at")
    search_fields = ("key",)
