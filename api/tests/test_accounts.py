from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, CreditCardStatement, Transaction

User = get_user_model()


class AccountCRUDTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)

    def test_list_accounts(self):
        Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        response = self.client.get(reverse('api:account-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)

    def test_create_account(self):
        response = self.client.post(reverse('api:account-list'), {
            'name': 'Checking', 'account_type': Account.AccountType.CHECKING,
            'current_balance': '1000.00', 'is_active': True,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(Account.objects.filter(name='Checking').exists())

    def test_update_account_balance(self):
        account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        response = self.client.patch(reverse('api:account-detail', args=[account.pk]),
                                      {'current_balance': '1200.00'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('1200.00'))

    def test_delete_account_without_history(self):
        account = Account.objects.create(name='Checking', current_balance=Decimal('0.00'))
        response = self.client.delete(reverse('api:account-detail', args=[account.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Account.objects.filter(pk=account.pk).exists())

    def test_delete_account_with_history_returns_400_not_500(self):
        account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        services.create_transaction(account=account, category=None, direction=Transaction.Direction.OUT,
                                     amount=Decimal('10.00'), description='x', due_date=date(2026, 1, 1),
                                     executed=True)
        response = self.client.delete(reverse('api:account-detail', args=[account.pk]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Account.objects.filter(pk=account.pk).exists())


class CreditCardBootstrapActionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.card = Account.objects.create(name='Visa', account_type=Account.AccountType.CREDIT_CARD,
                                            cut_day=24, payment_due_day=5)

    def test_bootstrap_statement_sets_balance_and_creates_statement(self):
        response = self.client.post(reverse('api:account-bootstrap-statement', args=[self.card.pk]), {
            'amount_owed': '500.00', 'due_date': '2026-09-05',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal('-500.00'))
        self.assertTrue(CreditCardStatement.objects.filter(account=self.card, statement_balance=Decimal('500.00')).exists())

    def test_bootstrap_statement_rejects_non_credit_card_account(self):
        checking = Account.objects.create(name='Checking', account_type=Account.AccountType.CHECKING)
        response = self.client.post(reverse('api:account-bootstrap-statement', args=[checking.pk]), {
            'amount_owed': '500.00', 'due_date': '2026-09-05',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CategoryCRUDTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)

    def test_create_and_list_category(self):
        response = self.client.post(reverse('api:category-list'), {'name': 'Groceries'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.get(reverse('api:category-list'))
        self.assertEqual(response.data['count'], 1)

    def test_update_and_delete_category(self):
        response = self.client.post(reverse('api:category-list'), {'name': 'Groceries'})
        pk = response.data['id']
        response = self.client.patch(reverse('api:category-detail', args=[pk]), {'name': 'Food'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.delete(reverse('api:category-detail', args=[pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
