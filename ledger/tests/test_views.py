from datetime import date, timedelta
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


class TransactionListSortingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.banana = services.create_transaction(account=self.account, category=None,
                                                    direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                                    description='Banana', due_date=date(2026, 1, 3))
        self.apple = services.create_transaction(account=self.account, category=None,
                                                   direction=Transaction.Direction.OUT, amount=Decimal('30.00'),
                                                   description='Apple', due_date=date(2026, 1, 1))
        self.cherry = services.create_transaction(account=self.account, category=None,
                                                    direction=Transaction.Direction.OUT, amount=Decimal('20.00'),
                                                    description='Cherry', due_date=date(2026, 1, 2))

    def ids(self, response):
        return [t.pk for t in response.context['transactions']]

    def test_default_sort_is_most_recently_active_first(self):
        response = self.client.get(reverse('ledger:transaction_list'))
        self.assertEqual(self.ids(response), [self.banana.pk, self.cherry.pk, self.apple.pk])
        self.assertIsNone(response.context['current_sort_column'])

    def test_default_sort_uses_executed_date_over_due_date_when_executed(self):
        # apple is due Jan 1 but actually executed Jan 10 — recency ordering puts it FIRST (most
        # recently active), even though its due_date is the earliest of the three.
        services.execute_transaction(self.apple, executed_date=date(2026, 1, 10))
        response = self.client.get(reverse('ledger:transaction_list'))
        self.assertEqual(self.ids(response), [self.apple.pk, self.banana.pk, self.cherry.pk])

    def test_due_date_column_sort_is_still_available_explicitly(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': 'due_date'})
        self.assertEqual(self.ids(response), [self.apple.pk, self.cherry.pk, self.banana.pk])
        self.assertEqual(response.context['current_sort_column'], 'due_date')
        self.assertFalse(response.context['current_sort_descending'])

    def test_sort_by_amount_ascending(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': 'amount'})
        self.assertEqual(self.ids(response), [self.banana.pk, self.cherry.pk, self.apple.pk])

    def test_sort_by_amount_descending(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': '-amount'})
        self.assertEqual(self.ids(response), [self.apple.pk, self.cherry.pk, self.banana.pk])

    def test_sort_by_description(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': 'description'})
        self.assertEqual(self.ids(response), [self.apple.pk, self.banana.pk, self.cherry.pk])

    def test_unknown_sort_column_falls_back_to_default_rather_than_crashing(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': 'not-a-real-column'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['current_sort_column'])

    def test_sort_link_toggles_direction_for_the_currently_sorted_column(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': 'amount'})
        self.assertEqual(response.context['sort_links']['amount'], '-amount')

    def test_sort_link_defaults_to_ascending_for_other_columns(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'sort': '-amount'})
        self.assertEqual(response.context['sort_links']['due_date'], 'due_date')

    def test_sort_by_account_and_status_do_not_crash(self):
        for sort in ('account', '-account', 'category', 'status', '-status'):
            response = self.client.get(reverse('ledger:transaction_list'), {'sort': sort})
            self.assertEqual(response.status_code, 200)


class TransactionListFutureVisibilityTests(TestCase):
    """A recurring template alone generates a 12-month horizon of pending occurrences, which
    would otherwise swamp what's meant to be a record of actual activity — future-dated pending
    transactions are hidden by default, with an explicit way to reveal them."""

    TODAY = date(2026, 3, 15)

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        with patch('ledger.views.timezone.localdate', return_value=self.TODAY):
            self.past_pending = services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('10.00'), description='overdue bill', due_date=date(2026, 3, 1))
            self.today_pending = services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('10.00'), description='due today', due_date=self.TODAY)
            self.future_pending = services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('10.00'), description='future bill', due_date=date(2026, 4, 1))
            self.executed_with_future_due_date = services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('10.00'), description='paid ahead', due_date=date(2026, 4, 1),
                executed=True, executed_date=self.TODAY)

    def ids(self, response):
        return {t.pk for t in response.context['transactions']}

    def get(self, **params):
        with patch('ledger.views.timezone.localdate', return_value=self.TODAY):
            return self.client.get(reverse('ledger:transaction_list'), params)

    def test_future_pending_hidden_by_default(self):
        response = self.get()
        ids = self.ids(response)
        self.assertIn(self.past_pending.pk, ids)
        self.assertIn(self.today_pending.pk, ids)
        self.assertNotIn(self.future_pending.pk, ids)
        self.assertFalse(response.context['show_future'])
        self.assertEqual(response.context['future_hidden_count'], 1)

    def test_executed_transaction_with_future_due_date_is_not_hidden(self):
        # only a PENDING transaction with a future due_date is a "plan" worth hiding — an
        # executed one already happened, regardless of what its due_date says.
        response = self.get()
        self.assertIn(self.executed_with_future_due_date.pk, self.ids(response))

    def test_show_future_reveals_everything(self):
        response = self.get(future='show')
        self.assertIn(self.future_pending.pk, self.ids(response))
        self.assertTrue(response.context['show_future'])
        self.assertEqual(response.context['future_hidden_count'], 0)

    def test_no_banner_when_nothing_is_hidden(self):
        Transaction.objects.filter(pk=self.future_pending.pk).delete()
        response = self.get()
        self.assertNotContains(response, 'Show future')


class TransactionListPaginationTests(TestCase):
    PAGE_SIZE = 50

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('100000.00'))
        for i in range(75):
            services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('1.00'), description=f'txn {i}', due_date=date(2026, 1, 1) + timedelta(days=i),
            )

    def test_first_page_has_page_size_items(self):
        response = self.client.get(reverse('ledger:transaction_list'))
        self.assertEqual(len(response.context['transactions']), self.PAGE_SIZE)
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 75)

    def test_second_page_has_remaining_items(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'page': 2})
        self.assertEqual(len(response.context['transactions']), 25)

    def test_out_of_range_page_returns_last_page_instead_of_404(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'page': 999})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 2)

    def test_non_integer_page_returns_first_page_instead_of_500(self):
        response = self.client.get(reverse('ledger:transaction_list'), {'page': 'abc'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 1)

    def test_pagination_controls_hidden_when_everything_fits_on_one_page(self):
        Transaction.objects.all().delete()
        response = self.client.get(reverse('ledger:transaction_list'))
        self.assertNotContains(response, 'Transactions pages')
