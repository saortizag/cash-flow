from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()


class ExpenseTotalsByPeriodTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('10000.00'))

    def make_expense(self, executed_date, amount, due_date=None):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal(amount), description='x', due_date=due_date or executed_date,
        )
        return services.execute_transaction(txn, executed_date=executed_date)

    def test_unknown_resolution_raises(self):
        with self.assertRaises(ValueError):
            services.expense_totals_by_period('year')

    def test_day_resolution_window_and_zero_fill(self):
        today = date(2026, 3, 15)
        self.make_expense(today, '50.00')
        self.make_expense(today - timedelta(days=5), '20.00')
        periods = services.expense_totals_by_period('day', today=today)
        self.assertEqual(len(periods), 30)
        self.assertEqual(periods[-1]['date'], today)
        self.assertEqual(periods[-1]['total'], Decimal('50.00'))
        self.assertEqual(periods[-6]['date'], today - timedelta(days=5))
        self.assertEqual(periods[-6]['total'], Decimal('20.00'))
        self.assertEqual(periods[-2]['total'], Decimal('0.00'))  # zero-filled gap
        self.assertEqual(periods[0]['date'], today - timedelta(days=29))

    def test_week_resolution_buckets_by_monday_and_sums_within_week(self):
        today = date(2026, 3, 18)  # a Wednesday
        monday = today - timedelta(days=today.weekday())
        self.make_expense(monday, '30.00')
        self.make_expense(monday + timedelta(days=3), '15.00')  # same ISO week, later day
        periods = services.expense_totals_by_period('week', today=today)
        self.assertEqual(len(periods), 12)
        self.assertEqual(periods[-1]['date'], monday)
        self.assertEqual(periods[-1]['total'], Decimal('45.00'))

    def test_month_resolution_buckets_by_first_of_month(self):
        today = date(2026, 3, 20)
        self.make_expense(date(2026, 3, 1), '100.00')
        self.make_expense(date(2026, 3, 31), '25.00')
        self.make_expense(date(2026, 2, 15), '10.00')
        periods = services.expense_totals_by_period('month', today=today)
        self.assertEqual(len(periods), 12)
        self.assertEqual(periods[-1]['date'], date(2026, 3, 1))
        self.assertEqual(periods[-1]['total'], Decimal('125.00'))
        self.assertEqual(periods[-2]['date'], date(2026, 2, 1))
        self.assertEqual(periods[-2]['total'], Decimal('10.00'))

    def test_unexecuted_transaction_excluded(self):
        today = date(2026, 3, 15)
        services.create_transaction(account=self.account, category=None, direction=Transaction.Direction.OUT,
                                     amount=Decimal('999.00'), description='x', due_date=today)
        periods = services.expense_totals_by_period('day', today=today)
        self.assertEqual(periods[-1]['total'], Decimal('0.00'))

    def test_income_direction_excluded(self):
        today = date(2026, 3, 15)
        txn = services.create_transaction(account=self.account, category=None, direction=Transaction.Direction.IN,
                                           amount=Decimal('500.00'), description='salary', due_date=today)
        services.execute_transaction(txn, executed_date=today)
        periods = services.expense_totals_by_period('day', today=today)
        self.assertEqual(periods[-1]['total'], Decimal('0.00'))

    def test_transfer_leg_excluded(self):
        """A transfer moves money between the user's own accounts — it isn't spending, and
        counting it would also double-count a credit card payment on top of the purchases it
        settles (already counted as ordinary OUT transactions on the card)."""
        today = date(2026, 3, 15)
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        services.create_transfer(from_account=self.account, to_account=savings, amount=Decimal('300.00'),
                                  description='move', due_date=today, executed=True, executed_date=today)
        periods = services.expense_totals_by_period('day', today=today)
        self.assertEqual(periods[-1]['total'], Decimal('0.00'))

    def test_bucketed_by_executed_date_not_due_date(self):
        today = date(2026, 3, 15)
        due = date(2026, 3, 1)
        self.make_expense(today, '40.00', due_date=due)
        periods = services.expense_totals_by_period('day', today=today)
        self.assertEqual(periods[-1]['total'], Decimal('40.00'))
        self.assertEqual(periods[-15]['date'], due)
        self.assertEqual(periods[-15]['total'], Decimal('0.00'))


class DashboardExpenseChartTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_dashboard_includes_expense_chart_data(self):
        today = date(2026, 3, 15)
        txn = services.create_transaction(account=self.account, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('75.00'), description='x', due_date=today)
        services.execute_transaction(txn, executed_date=today)
        with patch('ledger.services.timezone.localdate', return_value=today):
            response = self.client.get(reverse('ledger:dashboard'))
        self.assertEqual(response.status_code, 200)
        charts = response.context['expense_charts']
        self.assertEqual(set(charts.keys()), {'day', 'week', 'month'})
        self.assertEqual(charts['day'][-1]['total'], Decimal('75.00'))
        self.assertEqual(charts['day'][-1]['height_pct'], 100)
        self.assertContains(response, 'res-day')
        self.assertContains(response, 'expense-bar')

    def test_all_zero_series_has_zero_height_bars_not_a_crash(self):
        response = self.client.get(reverse('ledger:dashboard'))
        self.assertEqual(response.status_code, 200)
        for bar in response.context['expense_charts']['day']:
            self.assertEqual(bar['height_pct'], 0)
