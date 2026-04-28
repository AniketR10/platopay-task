"""
Idempotently register the periodic Django-Q schedules required by the payout engine.

Run after migrations on a fresh deploy:
    python manage.py setup_schedules
"""
from django.core.management.base import BaseCommand
from django_q.models import Schedule


SCHEDULES = [
    {
        "name": "retry_stuck_payouts",
        "func": "ledger.tasks.retry_stuck_payouts",
        "schedule_type": Schedule.MINUTES,
        "minutes": 1,
        "repeats": -1,
    },
    {
        "name": "cleanup_expired_idempotency_keys",
        "func": "ledger.tasks.cleanup_expired_idempotency_keys",
        "schedule_type": Schedule.HOURLY,
        "repeats": -1,
    },
]


class Command(BaseCommand):
    help = "Create or update the periodic schedules used by the payout engine."

    def handle(self, *args, **options):
        for spec in SCHEDULES:
            obj, created = Schedule.objects.update_or_create(
                name=spec["name"],
                defaults={k: v for k, v in spec.items() if k != "name"},
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{verb} schedule: {obj.name} -> {obj.func}"))
