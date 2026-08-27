from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()


class TransactionCreateTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_create_unexecuted_does_not_touch_balance(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'account': self.checking.pk, 'direction': Transaction.Direction.OUT,
            'amount': '50.00', 'description': 'Groceries', 'due_date': '2026-08-20',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertFalse(response.data['executed'])
        self.checking.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('1000.00'))

    def test_create_executed_updates_balance(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'account': self.checking.pk, 'direction': Transaction.Direction.OUT,
            'amount': '50.00', 'description': 'Groceries', 'due_date': '2026-08-20',
            'executed': True,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data['executed'])
        self.checking.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('950.00'))

    def test_create_executed_without_account_returns_400_not_500(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'direction': Transaction.Direction.OUT, 'amount': '50.00',
            'description': 'x', 'due_date': '2026-08-20', 'executed': True,
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_executed_without_due_date_defaults_to_executed_date(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'account': self.checking.pk, 'direction': Transaction.Direction.OUT,
            'amount': '50.00', 'description': 'Groceries', 'executed': True,
            'executed_date': '2026-08-20',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['due_date'], '2026-08-20')

    def test_create_unexecuted_without_due_date_returns_400_not_500(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'account': self.checking.pk, 'direction': Transaction.Direction.OUT,
            'amount': '50.00', 'description': 'Groceries',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TransactionListFilterTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.pending = services.create_transaction(
            account=self.checking, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('10.00'), description='pending', due_date=date(2026, 8, 20))
        self.executed = services.create_transaction(
            account=self.checking, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('20.00'), description='executed', due_date=date(2026, 8, 21), executed=True)
        self.unassigned = services.create_transaction(
            account=None, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('30.00'), description='unassigned', due_date=date(2026, 8, 22))

    def test_filter_by_executed_false(self):
        response = self.client.get(reverse('api:transaction-list'), {'executed': 'false'})
        ids = {row['id'] for row in response.data['results']}
        self.assertEqual(ids, {self.pending.pk, self.unassigned.pk})

    def test_filter_by_account_unassigned(self):
        response = self.client.get(reverse('api:transaction-list'), {'account': 'unassigned'})
        ids = {row['id'] for row in response.data['results']}
        self.assertEqual(ids, {self.unassigned.pk})

    def test_filter_by_account_id(self):
        response = self.client.get(reverse('api:transaction-list'), {'account': self.checking.pk})
        ids = {row['id'] for row in response.data['results']}
        self.assertEqual(ids, {self.pending.pk, self.executed.pk})


class TransactionUpdateDeleteTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))

    def test_full_edit_allowed_when_unexecuted(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20))
        response = self.client.patch(reverse('api:transaction-detail', args=[txn.pk]), {'amount': '99.00'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        txn.refresh_from_db()
        self.assertEqual(txn.amount, Decimal('99.00'))

    def test_amount_change_rejected_when_executed(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20),
                                           executed=True)
        response = self.client.patch(reverse('api:transaction-detail', args=[txn.pk]), {'amount': '99.00'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        txn.refresh_from_db()
        self.assertEqual(txn.amount, Decimal('10.00'))

    def test_open_field_edit_allowed_when_executed(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20),
                                           executed=True)
        response = self.client.patch(reverse('api:transaction-detail', args=[txn.pk]), {'description': 'updated'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        txn.refresh_from_db()
        self.assertEqual(txn.description, 'updated')

    def test_delete_blocked_when_executed(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20),
                                           executed=True)
        response = self.client.delete(reverse('api:transaction-detail', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Transaction.objects.filter(pk=txn.pk).exists())

    def test_delete_allowed_when_unexecuted(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20))
        response = self.client.delete(reverse('api:transaction-detail', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_execute_action_updates_balance(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20))
        response = self.client.post(reverse('api:transaction-execute', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.checking.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('990.00'))

    def test_execute_twice_returns_400(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20),
                                           executed=True)
        response = self.client.post(reverse('api:transaction-execute', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unexecute_action_reverses_balance(self):
        txn = services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20),
                                           executed=True)
        response = self.client.post(reverse('api:transaction-unexecute', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.checking.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('1000.00'))

    def test_assign_account_action(self):
        txn = services.create_transaction(account=None, category=None, direction=Transaction.Direction.OUT,
                                           amount=Decimal('10.00'), description='x', due_date=date(2026, 8, 20))
        response = self.client.post(reverse('api:transaction-assign-account', args=[txn.pk]),
                                     {'account': self.checking.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        txn.refresh_from_db()
        self.assertEqual(txn.account, self.checking)


class TransferLegGuardTests(APITestCase):
    """A transfer leg must never be individually edited/executed/deleted
    through the plain transaction endpoints — only assign-account is exempt
    (that's how an auto-generated credit-card payment obligation gets
    funded). Mirrors ledger.tests.test_transfers.TransferLegViewGuardTests."""

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('500.00'))
        self.transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                                   amount=Decimal('100.00'), description='move',
                                                   due_date=date(2026, 1, 1))

    def test_update_rejected(self):
        response = self.client.patch(reverse('api:transaction-detail', args=[self.transfer.out_leg_id]),
                                      {'amount': '999.00'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_rejected(self):
        response = self.client.delete(reverse('api:transaction-detail', args=[self.transfer.out_leg_id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_execute_rejected(self):
        response = self.client.post(reverse('api:transaction-execute', args=[self.transfer.out_leg_id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unexecute_rejected(self):
        services.execute_transfer(self.transfer)
        response = self.client.post(reverse('api:transaction-unexecute', args=[self.transfer.in_leg_id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.transfer.in_leg.refresh_from_db()
        self.assertTrue(self.transfer.in_leg.executed)

    def test_assign_account_is_allowed_on_an_unassigned_leg(self):
        unassigned_transfer = services.create_transfer(from_account=None, to_account=self.savings,
                                                         amount=Decimal('50.00'), description='pay',
                                                         due_date=date(2026, 1, 1))
        response = self.client.post(
            reverse('api:transaction-assign-account', args=[unassigned_transfer.out_leg_id]),
            {'account': self.checking.pk},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        unassigned_transfer.out_leg.refresh_from_db()
        self.assertEqual(unassigned_transfer.out_leg.account, self.checking)
