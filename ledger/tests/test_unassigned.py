from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()


class UnassignedTransactionServiceTests(TestCase):
    def setUp(self):
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_create_unassigned_transaction(self):
        txn = services.create_transaction(
            account=None, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('500.00'), description='big future bill', due_date=date(2026, 6, 1),
        )
        self.assertIsNone(txn.account_id)
        self.assertFalse(txn.executed)

    def test_cannot_execute_unassigned_transaction(self):
        txn = services.create_transaction(
            account=None, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('500.00'), description='big future bill', due_date=date(2026, 6, 1),
        )
        with self.assertRaises(ValidationError):
            services.execute_transaction(txn)

    def test_assign_account_then_execute_works(self):
        txn = services.create_transaction(
            account=None, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('500.00'), description='big future bill', due_date=date(2026, 6, 1),
        )
        services.assign_account(txn, self.checking)
        services.execute_transaction(txn)
        self.checking.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('500.00'))

    def test_assign_account_raises_if_already_executed(self):
        txn = services.create_transaction(
            account=self.checking, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='x', due_date=date(2026, 1, 1), executed=True,
        )
        with self.assertRaises(ValidationError):
            services.assign_account(txn, self.checking)

    def test_assign_account_on_stale_object_after_concurrent_assign_is_rejected(self):
        # Regression: assign_account used to check account_id on the caller's
        # in-memory object instead of a fresh locked read, so a second,
        # stale-object assignment would silently overwrite the first with no
        # error — matching the same class of bug already fixed for
        # execute_transaction/update_transaction_full/delete_transaction.
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        txn = services.create_transaction(
            account=None, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('500.00'), description='big future bill', due_date=date(2026, 6, 1),
        )
        fresh = Transaction.objects.get(pk=txn.pk)
        stale = Transaction.objects.get(pk=txn.pk)

        services.assign_account(fresh, self.checking)
        with self.assertRaises(ValidationError):
            services.assign_account(stale, savings)

        txn.refresh_from_db()
        self.assertEqual(txn.account, self.checking)


class UnassignedProjectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        Transaction.objects.create(account=self.checking, direction=Transaction.Direction.OUT,
                                    amount=Decimal('50.00'), due_date=date(2026, 2, 1))
        self.unassigned = Transaction.objects.create(
            account=None, direction=Transaction.Direction.OUT, amount=Decimal('200.00'),
            description='unassigned bill', due_date=date(2026, 2, 15),
        )

    def test_unassigned_counted_in_combined_total_not_per_account(self):
        response = self.client.get(reverse('ledger:projection'), {'target_date': '2026-03-01'})
        self.assertEqual(response.status_code, 200)
        # combined = 1000 (checking current) - 50 (checking's own pending) - 200 (unassigned) = 750
        self.assertEqual(response.context['combined_projected'], Decimal('750.00'))
        self.assertEqual(response.context['unassigned_total'], Decimal('-200.00'))
        for result in response.context['results']:
            for row in result['rows']:
                self.assertNotEqual(row['transaction'].pk, self.unassigned.pk)

    def test_unassigned_excluded_when_filtering_to_one_account(self):
        response = self.client.get(reverse('ledger:projection'), {
            'target_date': '2026-03-01', 'account': self.checking.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['unassigned_rows'], [])
        self.assertEqual(response.context['unassigned_total'], Decimal('0.00'))
        # combined == the single account's own projection when one is picked, unaffected by the unassigned obligation
        self.assertEqual(response.context['combined_projected'], Decimal('950.00'))


class UnassignedListingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.unassigned = Transaction.objects.create(
            account=None, direction=Transaction.Direction.OUT, amount=Decimal('200.00'),
            description='unassigned bill', due_date=date(2026, 2, 15),
        )

    def test_transaction_list_unassigned_filter(self):
        Transaction.objects.create(account=self.checking, direction=Transaction.Direction.OUT,
                                    amount=Decimal('10.00'), due_date=date(2026, 1, 1))
        response = self.client.get(reverse('ledger:transaction_list'), {'account': 'unassigned'})
        self.assertEqual(response.status_code, 200)
        pks = {t.pk for t in response.context['transactions']}
        self.assertEqual(pks, {self.unassigned.pk})

    def test_dashboard_shows_unassigned_section(self):
        response = self.client.get(reverse('ledger:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.unassigned, list(response.context['unassigned']))

    def test_assign_account_view(self):
        response = self.client.post(
            reverse('ledger:transaction_assign_account', args=[self.unassigned.pk]),
            {'account': self.checking.pk},
        )
        self.assertRedirects(response, reverse('ledger:transaction_list'))
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.account, self.checking)
