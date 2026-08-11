from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ledger.models import Account

User = get_user_model()


class SummaryViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)

    def test_summary_reflects_assets_and_card_debt(self):
        Account.objects.create(name='Checking', account_type=Account.AccountType.CHECKING,
                                current_balance=Decimal('1000.00'))
        Account.objects.create(name='Visa', account_type=Account.AccountType.CREDIT_CARD,
                                current_balance=Decimal('-250.00'))
        response = self.client.get(reverse('api:summary'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data['total_assets']), Decimal('1000.00'))
        self.assertEqual(Decimal(response.data['total_card_debt']), Decimal('250.00'))
        self.assertEqual(Decimal(response.data['net_worth']), Decimal('750.00'))
        self.assertEqual(len(response.data['accounts']), 2)

    def test_summary_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('api:summary'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
