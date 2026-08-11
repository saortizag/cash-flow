from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, RecurringTransaction, Transaction

User = get_user_model()


class RecurringTransactionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.today = timezone.localdate()

    def make_recurring(self):
        return RecurringTransaction.objects.create(
            account=self.checking, direction=RecurringTransaction.Direction.OUT, amount=Decimal('20.00'),
            description='Netflix', frequency=RecurringTransaction.Frequency.MONTHLY, interval=1,
            start_date=self.today,
        )

    def test_create_generates_occurrences(self):
        response = self.client.post(reverse('api:recurringtransaction-list'), {
            'account': self.checking.pk, 'direction': RecurringTransaction.Direction.OUT,
            'amount': '20.00', 'description': 'Netflix', 'frequency': RecurringTransaction.Frequency.MONTHLY,
            'interval': 1, 'start_date': self.today.isoformat(), 'is_active': True,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        recurring = RecurringTransaction.objects.get(pk=response.data['id'])
        self.assertTrue(recurring.occurrences.exists())
        self.assertIsNotNone(recurring.generated_until)

    def test_end_date_before_start_date_returns_400(self):
        response = self.client.post(reverse('api:recurringtransaction-list'), {
            'account': self.checking.pk, 'direction': RecurringTransaction.Direction.OUT,
            'amount': '20.00', 'description': 'Netflix', 'frequency': RecurringTransaction.Frequency.MONTHLY,
            'interval': 1, 'start_date': self.today.isoformat(),
            'end_date': (self.today - timedelta(days=1)).isoformat(),
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_regenerates_future_occurrences_with_new_amount(self):
        recurring = self.make_recurring()
        services.ensure_recurring_horizon(recurring, today=self.today)

        response = self.client.patch(reverse('api:recurringtransaction-detail', args=[recurring.pk]),
                                      {'amount': '35.00'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        recurring.refresh_from_db()
        self.assertTrue(recurring.occurrences.filter(executed=False, amount=Decimal('35.00')).exists())

    def test_delete_removes_template_and_future_unexecuted_occurrences(self):
        recurring = self.make_recurring()
        services.ensure_recurring_horizon(recurring, today=self.today)
        response = self.client.delete(reverse('api:recurringtransaction-detail', args=[recurring.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(RecurringTransaction.objects.filter(pk=recurring.pk).exists())
        self.assertFalse(Transaction.objects.filter(due_date__gte=self.today, executed=False,
                                                      description='Netflix').exists())

    def test_deactivate_action(self):
        recurring = self.make_recurring()
        services.ensure_recurring_horizon(recurring, today=self.today)
        response = self.client.post(reverse('api:recurringtransaction-deactivate', args=[recurring.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        recurring.refresh_from_db()
        self.assertFalse(recurring.is_active)
        self.assertFalse(recurring.occurrences.filter(executed=False, due_date__gte=self.today).exists())
