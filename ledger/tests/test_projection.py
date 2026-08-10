from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()


class ProjectionMathTests(TestCase):
    """Exercises the same summation logic the projection view uses, directly
    against the model layer, so the arithmetic is verified independent of
    rendering."""

    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def project(self, target_date):
        pending = Transaction.objects.filter(
            account=self.account, executed=False, due_date__lte=target_date,
        ).order_by('due_date', 'id')
        running = self.account.current_balance
        rows = []
        for txn in pending:
            running += services.signed_amount(txn.direction, txn.amount)
            rows.append((txn, running))
        return rows, running

    def test_projection_sums_only_unexecuted_due_by_target(self):
        Transaction.objects.create(account=self.account, direction=Transaction.Direction.OUT,
                                    amount=Decimal('100.00'), due_date=date(2026, 2, 1))
        Transaction.objects.create(account=self.account, direction=Transaction.Direction.IN,
                                    amount=Decimal('300.00'), due_date=date(2026, 2, 15))
        # due after target date — must be excluded
        Transaction.objects.create(account=self.account, direction=Transaction.Direction.OUT,
                                    amount=Decimal('500.00'), due_date=date(2026, 4, 1))

        rows, projected = self.project(date(2026, 3, 1))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], Decimal('900.00'))   # 1000 - 100
        self.assertEqual(rows[1][1], Decimal('1200.00'))  # 900 + 300
        self.assertEqual(projected, Decimal('1200.00'))

    def test_executed_transactions_are_excluded_from_projection(self):
        txn = Transaction.objects.create(account=self.account, direction=Transaction.Direction.OUT,
                                          amount=Decimal('100.00'), due_date=date(2026, 2, 1))
        services.execute_transaction(txn, executed_date=date(2026, 1, 15))
        self.account.refresh_from_db()

        rows, projected = self.project(date(2026, 3, 1))

        self.assertEqual(rows, [])
        self.assertEqual(projected, self.account.current_balance)
        self.assertEqual(projected, Decimal('900.00'))

    def test_no_pending_transactions_projects_current_balance_unchanged(self):
        rows, projected = self.project(date(2026, 12, 31))
        self.assertEqual(rows, [])
        self.assertEqual(projected, Decimal('1000.00'))


class ProjectionViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        Transaction.objects.create(account=self.account, direction=Transaction.Direction.OUT,
                                    amount=Decimal('100.00'), due_date=date(2026, 2, 1))

    def test_projection_page_renders_expected_totals(self):
        response = self.client.get(reverse('ledger:projection'), {
            'target_date': '2026-03-01', 'account': self.account.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['combined_current'], Decimal('1000.00'))
        self.assertEqual(response.context['combined_projected'], Decimal('900.00'))

    def test_projection_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('ledger:projection'))
        self.assertEqual(response.status_code, 302)
