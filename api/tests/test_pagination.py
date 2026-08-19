from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()


class TransactionListPaginationTests(APITestCase):
    """PAGE_SIZE=50 is a REST_FRAMEWORK-wide default (settings.py), so this was already active
    for every list endpoint before this feature — these tests lock it in as an explicit,
    intentional requirement for /transactions/ specifically rather than an incidental default."""

    PAGE_SIZE = 50

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('100000.00'))
        for i in range(75):
            services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('1.00'), description=f'txn {i}', due_date=date(2026, 1, 1) + timedelta(days=i),
            )

    def test_first_page_has_page_size_items_and_pagination_envelope(self):
        response = self.client.get(reverse('api:transaction-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 75)
        self.assertEqual(len(response.data['results']), self.PAGE_SIZE)
        self.assertIsNotNone(response.data['next'])
        self.assertIsNone(response.data['previous'])

    def test_second_page_has_remaining_items(self):
        response = self.client.get(reverse('api:transaction-list'), {'page': 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 25)
        self.assertIsNone(response.data['next'])
        self.assertIsNotNone(response.data['previous'])

    def test_page_out_of_range_returns_404_not_500(self):
        response = self.client.get(reverse('api:transaction-list'), {'page': 999})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_default_ordering_is_due_date_ascending(self):
        response = self.client.get(reverse('api:transaction-list'))
        due_dates = [row['due_date'] for row in response.data['results']]
        self.assertEqual(due_dates, sorted(due_dates))
        self.assertEqual(due_dates[0], '2026-01-01')


class OtherListEndpointsPaginationTests(APITestCase):
    """Spot-check that the PAGE_SIZE=50 default really is global, not something this feature
    special-cased onto /transactions/ alone."""

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        for i in range(55):
            Account.objects.create(name=f'Account {i}', current_balance=Decimal('0.00'))

    def test_accounts_list_is_paginated_too(self):
        response = self.client.get(reverse('api:account-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 55)
        self.assertEqual(len(response.data['results']), 50)
