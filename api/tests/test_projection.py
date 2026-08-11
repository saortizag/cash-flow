from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()


class ProjectionViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.today = timezone.localdate()
        services.create_transaction(account=self.checking, category=None, direction=Transaction.Direction.OUT,
                                     amount=Decimal('50.00'), description='rent',
                                     due_date=self.today + timedelta(days=5))

    def test_summary_requires_target_date(self):
        response = self.client.get(reverse('api:projection-summary'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_summary_matches_service_output_and_omits_rows(self):
        target = self.today + timedelta(days=30)
        response = self.client.get(reverse('api:projection-summary'), {'target_date': target.isoformat()})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        expected = services.project_balances(target)
        self.assertEqual(Decimal(response.data['combined_current']), expected['combined_current'])
        self.assertEqual(Decimal(response.data['combined_projected']), expected['combined_projected'])
        self.assertNotIn('rows', response.data['results'][0])

    def test_detail_includes_running_balance_rows(self):
        target = self.today + timedelta(days=30)
        response = self.client.get(reverse('api:projection-detail'), {
            'target_date': target.isoformat(), 'account': self.checking.pk,
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data['results']), 1)
        rows = response.data['results'][0]['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(Decimal(rows[0]['running_balance']), Decimal('950.00'))

    def test_unassigned_obligation_only_folds_into_combined_view(self):
        services.create_transaction(account=None, category=None, direction=Transaction.Direction.OUT,
                                     amount=Decimal('75.00'), description='unassigned bill',
                                     due_date=self.today + timedelta(days=10))
        target = self.today + timedelta(days=30)

        combined = self.client.get(reverse('api:projection-detail'), {'target_date': target.isoformat()})
        self.assertEqual(len(combined.data['unassigned_rows']), 1)
        self.assertEqual(Decimal(combined.data['combined_projected']), Decimal('1000.00') - Decimal('50.00') - Decimal('75.00'))

        single = self.client.get(reverse('api:projection-detail'), {
            'target_date': target.isoformat(), 'account': self.checking.pk,
        })
        self.assertEqual(single.data['unassigned_rows'], [])
        self.assertEqual(Decimal(single.data['combined_projected']), Decimal('950.00'))
