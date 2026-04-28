"""
Seed the database with three merchants, bank accounts, and a credit history.

    python manage.py seed [--reset]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import BankAccount, IdempotencyKey, LedgerEntry, Merchant, Payout
from ledger.services import credit_merchant


SEED_MERCHANTS = [
    {
        "name": "Bombay Code Co.",
        "bank": ("Aniket Sharma", "****4271", "HDFC0000123"),
        # paise, description
        "credits": [
            (250_000, "Stripe payout · invoice #INV-1041"),
            (480_000, "Wise transfer · invoice #INV-1043"),
            (199_000, "PayPal · invoice #INV-1045"),
            (310_500, "Stripe payout · invoice #INV-1046"),
        ],
    },
    {
        "name": "Calicut Pixel Studio",
        "bank": ("Reema Nair", "****8810", "ICIC0000456"),
        "credits": [
            (180_000, "Stripe · invoice #PXS-204"),
            (75_000, "Bank wire · invoice #PXS-205"),
            (425_000, "Stripe · invoice #PXS-208"),
        ],
    },
    {
        "name": "Bengaluru Backend Labs",
        "bank": ("Rohit Iyer", "****9032", "AXIS0000789"),
        "credits": [
            (1_200_000, "Wire transfer · retainer May"),
            (650_000, "Stripe · invoice #BBL-32"),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed the database with merchants, bank accounts, and a credit history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing rows in dependency order before seeding.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            with transaction.atomic():
                IdempotencyKey.objects.all().delete()
                LedgerEntry.objects.all().delete()
                Payout.objects.all().delete()
                BankAccount.objects.all().delete()
                Merchant.objects.all().delete()
            self.stdout.write(self.style.WARNING("Cleared existing ledger data."))

        for spec in SEED_MERCHANTS:
            merchant, created = Merchant.objects.get_or_create(name=spec["name"])
            verb = "Created" if created else "Found"
            self.stdout.write(f"{verb} merchant: {merchant.name} ({merchant.id})")

            holder, masked, ifsc = spec["bank"]
            bank, _ = BankAccount.objects.get_or_create(
                merchant=merchant,
                account_holder_name=holder,
                defaults={"account_number_masked": masked, "ifsc": ifsc, "is_active": True},
            )

            # Idempotent-ish credit seeding: only seed if the merchant has zero credits yet.
            if not LedgerEntry.objects.filter(
                merchant=merchant, entry_type=LedgerEntry.EntryType.CREDIT
            ).exists():
                for amount, desc in spec["credits"]:
                    credit_merchant(merchant_id=merchant.id, amount_paise=amount, description=desc)
                self.stdout.write(f"  + {len(spec['credits'])} credits")
            else:
                self.stdout.write("  (skipping credits — already present)")

        self.stdout.write(self.style.SUCCESS("Seed complete."))
