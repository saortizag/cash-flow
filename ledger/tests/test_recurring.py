from datetime import date
from decimal import Decimal

from django.test import TestCase

from ledger import services
from ledger.models import Account, RecurringTransaction, Transaction


class RecurringGenerationTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def make_recurring(self, **overrides):
        defaults = dict(
            account=self.account, direction=RecurringTransaction.Direction.OUT,
            amount=Decimal('20.00'), description='Netflix',
            frequency=RecurringTransaction.Frequency.MONTHLY, interval=1,
            start_date=date(2026, 1, 31),
        )
        defaults.update(overrides)
        return RecurringTransaction.objects.create(**defaults)

    def test_month_end_dates_do_not_drift(self):
        recurring = self.make_recurring()
        services.ensure_recurring_horizon(recurring, horizon_months=4, today=date(2026, 1, 31))
        due_dates = list(
            recurring.occurrences.order_by('due_date').values_list('due_date', flat=True)
        )
        self.assertEqual(due_dates, [
            date(2026, 1, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
            date(2026, 4, 30),
            date(2026, 5, 31),
        ])

    def test_ensure_horizon_is_idempotent_same_day(self):
        recurring = self.make_recurring()
        first = services.ensure_recurring_horizon(recurring, horizon_months=6, today=date(2026, 1, 31))
        second = services.ensure_recurring_horizon(recurring, horizon_months=6, today=date(2026, 1, 31))
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def test_weekly_biweekly_via_interval(self):
        recurring = self.make_recurring(
            frequency=RecurringTransaction.Frequency.WEEKLY, interval=2, start_date=date(2026, 1, 5),
        )
        services.ensure_recurring_horizon(recurring, horizon_months=1, today=date(2026, 1, 5))
        due_dates = list(recurring.occurrences.order_by('due_date').values_list('due_date', flat=True))
        self.assertEqual(due_dates, [date(2026, 1, 5), date(2026, 1, 19), date(2026, 2, 2)])

    def test_regenerate_leaves_executed_occurrences_untouched(self):
        recurring = self.make_recurring(start_date=date(2026, 1, 1))
        services.ensure_recurring_horizon(recurring, horizon_months=3, today=date(2026, 1, 1))
        first_occurrence = recurring.occurrences.order_by('due_date').first()
        services.execute_transaction(first_occurrence, executed_date=date(2026, 1, 1))

        recurring.amount = Decimal('99.00')
        recurring.save()
        services.regenerate_future_occurrences(recurring, today=date(2026, 1, 1))

        first_occurrence.refresh_from_db()
        self.assertEqual(first_occurrence.amount, Decimal('20.00'))

        future = recurring.occurrences.filter(executed=False)
        self.assertTrue(future.exists())
        for occ in future:
            self.assertEqual(occ.amount, Decimal('99.00'))

    def test_regenerate_leaves_overdue_unexecuted_untouched(self):
        recurring = self.make_recurring(start_date=date(2026, 1, 1))
        services.ensure_recurring_horizon(recurring, horizon_months=3, today=date(2026, 3, 15))
        overdue = recurring.occurrences.filter(due_date__lt=date(2026, 3, 15), executed=False)
        self.assertTrue(overdue.exists())
        overdue_pks = set(overdue.values_list('pk', flat=True))

        recurring.amount = Decimal('99.00')
        recurring.save()
        services.regenerate_future_occurrences(recurring, today=date(2026, 3, 15))

        for pk in overdue_pks:
            occ = Transaction.objects.get(pk=pk)
            self.assertEqual(occ.amount, Decimal('20.00'))

    def test_deactivate_removes_only_future_unexecuted(self):
        recurring = self.make_recurring(start_date=date(2026, 1, 1))
        services.ensure_recurring_horizon(recurring, horizon_months=3, today=date(2026, 2, 15))

        overdue_occurrence = recurring.occurrences.filter(due_date__lt=date(2026, 2, 15)).first()
        future_occurrence = recurring.occurrences.filter(due_date__gte=date(2026, 2, 15)).first()
        self.assertIsNotNone(overdue_occurrence)
        self.assertIsNotNone(future_occurrence)

        services.deactivate_recurring(recurring, today=date(2026, 2, 15))

        recurring.refresh_from_db()
        self.assertFalse(recurring.is_active)
        self.assertTrue(Transaction.objects.filter(pk=overdue_occurrence.pk).exists())
        self.assertFalse(Transaction.objects.filter(pk=future_occurrence.pk).exists())

    def test_delete_recurring_sets_null_on_surviving_occurrences(self):
        recurring = self.make_recurring(start_date=date(2026, 1, 1))
        services.ensure_recurring_horizon(recurring, horizon_months=3, today=date(2026, 3, 15))
        overdue = recurring.occurrences.filter(due_date__lt=date(2026, 3, 15)).first()
        overdue_pk = overdue.pk

        services.delete_recurring(recurring, today=date(2026, 3, 15))

        surviving = Transaction.objects.get(pk=overdue_pk)
        self.assertIsNone(surviving.recurring_source_id)
        self.assertFalse(RecurringTransaction.objects.filter(pk=recurring.pk).exists())

    def test_inactive_template_is_a_horizon_noop(self):
        recurring = self.make_recurring(is_active=False, start_date=date(2026, 1, 1))
        created = services.ensure_recurring_horizon(recurring, horizon_months=3, today=date(2026, 1, 1))
        self.assertEqual(created, 0)
        self.assertFalse(recurring.occurrences.exists())


class RecurringResumeIndexRegressionTests(TestCase):
    """Regression coverage for a real bug: generation used to resume from
    `n = recurring.occurrences.count()`, assuming surviving rows form a
    contiguous index prefix. Executing an occurrence out of chronological
    order, or editing start_date/frequency, breaks that assumption and used to
    silently drop or misplace occurrences. The fix always walks from index 0
    (anchored to start_date) and relies on get_or_create to no-op on dates
    that already have a row, which is self-healing regardless of *why* a date
    is missing."""

    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def make_recurring(self, **overrides):
        defaults = dict(
            account=self.account, direction=RecurringTransaction.Direction.OUT,
            amount=Decimal('20.00'), description='Netflix',
            frequency=RecurringTransaction.Frequency.WEEKLY, interval=1,
            start_date=date(2026, 1, 1),
        )
        defaults.update(overrides)
        return RecurringTransaction.objects.create(**defaults)

    def test_executing_out_of_order_then_regenerating_does_not_drop_a_pending_occurrence(self):
        recurring = self.make_recurring(start_date=date(2026, 1, 1))
        services.ensure_recurring_horizon(recurring, horizon_months=1, today=date(2026, 1, 5))
        due_dates = list(recurring.occurrences.order_by('due_date').values_list('due_date', flat=True))
        # weekly from Jan 1: Jan1(idx0), Jan8(idx1), Jan15(idx2), Jan22(idx3), Jan29(idx4)
        self.assertIn(date(2026, 1, 15), due_dates)
        self.assertIn(date(2026, 1, 8), due_dates)

        # Execute the LATER occurrence (idx2) while the EARLIER one (idx1) stays pending.
        later = recurring.occurrences.get(due_date=date(2026, 1, 15))
        services.execute_transaction(later, executed_date=date(2026, 1, 5))

        # A template edit triggers regenerate_future_occurrences, which deletes
        # all unexecuted due_date >= today (drops idx1, idx3, idx4, ...) and
        # keeps idx0 (overdue) and idx2 (now executed).
        recurring.amount = Decimal('25.00')
        recurring.save()
        services.regenerate_future_occurrences(recurring, today=date(2026, 1, 5))

        due_dates_after = set(
            recurring.occurrences.order_by('due_date').values_list('due_date', flat=True)
        )
        self.assertIn(date(2026, 1, 8), due_dates_after,
                       'the pending Jan 8 occurrence must not silently vanish')

    def test_editing_start_date_and_frequency_generates_new_schedule_from_its_own_anchor(self):
        recurring = self.make_recurring(
            frequency=RecurringTransaction.Frequency.MONTHLY, start_date=date(2026, 1, 1),
        )
        services.ensure_recurring_horizon(recurring, horizon_months=2, today=date(2026, 1, 1))
        jan = recurring.occurrences.get(due_date=date(2026, 1, 1))
        feb = recurring.occurrences.get(due_date=date(2026, 2, 1))
        services.execute_transaction(jan, executed_date=date(2026, 1, 1))
        services.execute_transaction(feb, executed_date=date(2026, 2, 1))

        # Re-anchor the template to a new start_date and a different frequency.
        recurring.start_date = date(2026, 2, 15)
        recurring.frequency = RecurringTransaction.Frequency.WEEKLY
        recurring.save()
        services.regenerate_future_occurrences(recurring, today=date(2026, 2, 15))

        due_dates = set(
            recurring.occurrences.filter(executed=False).values_list('due_date', flat=True)
        )
        self.assertIn(date(2026, 2, 15), due_dates,
                       "the new schedule's own first occurrence must be generated, not skipped")
        self.assertIn(date(2026, 2, 22), due_dates,
                       "the new schedule's own second occurrence must be generated, not skipped")


class DeactivateReactivateRegressionTests(TestCase):
    """Regression coverage for a real bug: deactivate_recurring never reset
    generated_until, so reactivating a template through any path that doesn't
    also call regenerate_future_occurrences (e.g. flipping is_active back to
    True directly, which is exactly what the Django admin allowed) left the
    horizon cache stale and silently suppressed all future generation."""

    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.recurring = RecurringTransaction.objects.create(
            account=self.account, direction=RecurringTransaction.Direction.OUT,
            amount=Decimal('20.00'), description='Netflix',
            frequency=RecurringTransaction.Frequency.MONTHLY, interval=1,
            start_date=date(2026, 1, 1),
        )

    def test_reactivating_outside_the_edit_flow_still_regenerates(self):
        services.ensure_recurring_horizon(self.recurring, today=date(2026, 1, 1))
        self.assertTrue(self.recurring.occurrences.exists())

        services.deactivate_recurring(self.recurring, today=date(2026, 1, 1))
        self.recurring.refresh_from_db()
        self.assertFalse(self.recurring.is_active)
        self.assertIsNone(self.recurring.generated_until)
        self.assertFalse(self.recurring.occurrences.filter(executed=False).exists())

        # Simulate reactivation via a path that bypasses services entirely,
        # e.g. the Django admin's plain is_active field.
        self.recurring.is_active = True
        self.recurring.save()

        created = services.ensure_recurring_horizon(self.recurring, today=date(2026, 1, 1))
        self.assertGreater(created, 0)
        self.assertTrue(self.recurring.occurrences.filter(executed=False).exists())
