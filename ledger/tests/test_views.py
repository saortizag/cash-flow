from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ledger import services
from ledger.models import Account, RecurringTransaction, Transaction

User = get_user_model()


class TransactionListFilterRegressionTests(TestCase):
    """Regression coverage for a real bug: transaction_list passed raw GET
    filter params straight into `.filter(account_id=..., category_id=...)`
    with no validation, so a non-numeric value (e.g. a hand-edited or stale
    bookmarked URL) raised an uncaught ValueError -> unhandled 500. The fix
    ignores non-numeric filter values instead of crashing."""

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)

    def test_non_numeric_account_filter_does_not_500(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'account': 'abc'})
        self.assertEqual(response.status_code, 200)

    def test_non_numeric_category_filter_does_not_500(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'category': 'xyz'})
        self.assertEqual(response.status_code, 200)

    def test_valid_numeric_account_filter_still_works(self):
        account = Account.objects.create(name='Checking', current_balance=Decimal('0.00'))
        Transaction.objects.create(account=account, direction=Transaction.Direction.OUT,
                                    amount=Decimal('5.00'), due_date=date(2026, 1, 1))
        response = self.client.get(reverse('ledger:transaction_list'), {'account': account.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['transactions']), 1)


class RecurringAdminRoutingRegressionTests(TestCase):
    """Regression coverage for a real bug: RecurringTransactionAdmin had no
    save_model/delete_model override, so editing or deleting a template
    through Django admin bypassed services.regenerate_future_occurrences /
    services.delete_recurring entirely, leaving already-materialized future
    occurrences stale (wrong amount) or orphaned (surviving after the
    template that "deleted" them was gone).

    admin's save_model/delete_model call the services functions with no
    explicit `today`, so they fall back to the real wall-clock date. To keep
    this test deterministic regardless of when it's actually run (rather than
    picking a start_date that only works for whatever "today" happened to be
    when this was written), `timezone.localdate()` is frozen for the whole
    scenario via mock.patch."""

    ANCHOR = date(2026, 1, 1)

    def setUp(self):
        self.staff = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.client.force_login(self.staff)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        with patch('ledger.services.timezone.localdate', return_value=self.ANCHOR):
            self.recurring = RecurringTransaction.objects.create(
                account=self.account, direction=RecurringTransaction.Direction.OUT,
                amount=Decimal('20.00'), description='Netflix',
                frequency=RecurringTransaction.Frequency.MONTHLY, interval=1, start_date=self.ANCHOR,
            )
            services.ensure_recurring_horizon(self.recurring, today=self.ANCHOR)

    def test_editing_amount_via_admin_regenerates_future_occurrences(self):
        url = reverse('admin:ledger_recurringtransaction_change', args=[self.recurring.pk])
        with patch('ledger.services.timezone.localdate', return_value=self.ANCHOR):
            response = self.client.post(url, {
                'account': self.account.pk, 'category': '', 'direction': 'OUT', 'amount': '99.00',
                'description': 'Netflix', 'frequency': 'monthly', 'interval': '1',
                'start_date': self.ANCHOR.isoformat(), 'end_date': '', 'is_active': 'on',
            })
        self.assertEqual(response.status_code, 302, response.context['adminform'].form.errors if response.status_code == 200 else None)
        future = self.recurring.occurrences.filter(executed=False)
        self.assertTrue(future.exists())
        for occ in future:
            self.assertEqual(occ.amount, Decimal('99.00'))

    def test_deleting_via_admin_removes_future_occurrences(self):
        future_pks = list(self.recurring.occurrences.filter(executed=False).values_list('pk', flat=True))
        self.assertTrue(future_pks)

        url = reverse('admin:ledger_recurringtransaction_delete', args=[self.recurring.pk])
        with patch('ledger.services.timezone.localdate', return_value=self.ANCHOR):
            response = self.client.post(url, {'post': 'yes'})
        self.assertEqual(response.status_code, 302)

        self.assertFalse(Transaction.objects.filter(pk__in=future_pks).exists())
        self.assertFalse(RecurringTransaction.objects.filter(pk=self.recurring.pk).exists())
