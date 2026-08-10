from django.core.management.base import BaseCommand

from ledger.services import DEFAULT_HORIZON_MONTHS, ensure_recurring_horizon_for_all_active


class Command(BaseCommand):
    help = 'Materialize any missing Transaction occurrences for active RecurringTransaction templates.'

    def add_arguments(self, parser):
        parser.add_argument('--horizon-months', type=int, default=DEFAULT_HORIZON_MONTHS)

    def handle(self, *args, **options):
        results = ensure_recurring_horizon_for_all_active(horizon_months=options['horizon_months'])
        total = sum(results.values())
        self.stdout.write(self.style.SUCCESS(
            f'Generated {total} new occurrence(s) across {len(results)} active template(s).'
        ))
