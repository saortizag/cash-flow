from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, Transfer

User = get_user_model()


class TransferCRUDTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('500.00'))

    def test_create_transfer(self):
        response = self.client.post(reverse('api:transfer-list'), {
            'from_account': self.checking.pk, 'to_account': self.savings.pk,
            'amount': '100.00', 'description': 'move', 'due_date': '2026-01-01',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['from_account'], self.checking.pk)
        self.assertEqual(response.data['to_account'], self.savings.pk)
        self.assertFalse(response.data['executed'])

    def test_create_without_source_account(self):
        response = self.client.post(reverse('api:transfer-list'), {
            'to_account': self.savings.pk, 'amount': '100.00', 'description': 'pay', 'due_date': '2026-01-01',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data['from_account'])

    def test_self_transfer_rejected(self):
        response = self.client.post(reverse('api:transfer-list'), {
            'from_account': self.checking.pk, 'to_account': self.checking.pk,
            'amount': '100.00', 'description': 'move', 'due_date': '2026-01-01',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_executed_without_source_rejected(self):
        response = self.client.post(reverse('api:transfer-list'), {
            'to_account': self.savings.pk, 'amount': '100.00', 'description': 'pay',
            'due_date': '2026-01-01', 'executed': True,
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_amount_description_due_date(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        response = self.client.patch(reverse('api:transfer-detail', args=[transfer.pk]), {
            'amount': '150.00', 'description': 'updated', 'due_date': '2026-02-01',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        transfer.out_leg.refresh_from_db()
        transfer.in_leg.refresh_from_db()
        self.assertEqual(transfer.out_leg.amount, Decimal('150.00'))
        self.assertEqual(transfer.in_leg.amount, Decimal('150.00'))

    def test_update_rejected_when_executed(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1),
                                             executed=True)
        response = self.client.patch(reverse('api:transfer-detail', args=[transfer.pk]), {
            'amount': '999.00', 'description': 'x', 'due_date': '2026-01-01',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_transfer(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        response = self.client.delete(reverse('api:transfer-detail', args=[transfer.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_execute_moves_both_balances(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        response = self.client.post(reverse('api:transfer-execute', args=[transfer.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('900.00'))
        self.assertEqual(self.savings.current_balance, Decimal('600.00'))

    def test_execute_without_source_account_returns_400(self):
        transfer = services.create_transfer(from_account=None, to_account=self.savings,
                                             amount=Decimal('100.00'), description='pay', due_date=date(2026, 1, 1))
        response = self.client.post(reverse('api:transfer-execute', args=[transfer.pk]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unexecute_reverses_both_balances(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1),
                                             executed=True)
        response = self.client.post(reverse('api:transfer-unexecute', args=[transfer.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('1000.00'))
        self.assertEqual(self.savings.current_balance, Decimal('500.00'))
