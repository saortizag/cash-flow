from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from ledger import services
from ledger.forms import TransactionCreateForm, TransferCreateForm
from ledger.models import Account, Transaction

TODAY = date(2026, 3, 15)


class CreateTransactionDueDateDefaultingTests(TestCase):
    """due_date is required unless executed=True — logging something that already happened
    shouldn't also require separately typing the same date into a now-meaningless due-date
    field. See services.create_transaction's docstring."""

    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_executed_without_due_date_defaults_to_executed_date(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('10.00'), description='x', executed=True, executed_date=date(2026, 2, 1),
        )
        self.assertEqual(txn.due_date, date(2026, 2, 1))

    def test_executed_without_due_date_or_executed_date_defaults_to_today(self):
        with patch('ledger.services.timezone.localdate', return_value=TODAY):
            txn = services.create_transaction(
                account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('10.00'), description='x', executed=True,
            )
        self.assertEqual(txn.due_date, TODAY)
        self.assertEqual(txn.executed_date, TODAY)

    def test_executed_with_explicit_due_date_is_respected(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('10.00'), description='x', due_date=date(2026, 1, 1),
            executed=True, executed_date=date(2026, 1, 3),
        )
        self.assertEqual(txn.due_date, date(2026, 1, 1))
        self.assertEqual(txn.executed_date, date(2026, 1, 3))

    def test_unexecuted_without_due_date_raises(self):
        with self.assertRaises(ValidationError):
            services.create_transaction(account=self.account, category=None,
                                         direction=Transaction.Direction.OUT,
                                         amount=Decimal('10.00'), description='x')


class TransactionCreateFormDueDateTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def base_data(self, **overrides):
        data = {
            'account': self.account.pk, 'category': '', 'direction': Transaction.Direction.OUT,
            'amount': '10.00', 'description': 'x',
        }
        data.update(overrides)
        return data

    def test_executed_without_due_date_is_valid(self):
        form = TransactionCreateForm(data=self.base_data(executed=True, executed_date='2026-02-01'))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['due_date'], date(2026, 2, 1))

    def test_unexecuted_without_due_date_is_invalid(self):
        form = TransactionCreateForm(data=self.base_data())
        self.assertFalse(form.is_valid())
        self.assertIn('due_date', form.errors)

    def test_unexecuted_with_due_date_is_valid(self):
        form = TransactionCreateForm(data=self.base_data(due_date='2026-01-01'))
        self.assertTrue(form.is_valid(), form.errors)


class CreateTransferDueDateDefaultingTests(TestCase):
    def setUp(self):
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))

    def test_executed_without_due_date_defaults_to_executed_date(self):
        transfer = services.create_transfer(
            from_account=self.checking, to_account=self.savings, amount=Decimal('100.00'),
            description='move', executed=True, executed_date=date(2026, 2, 1),
        )
        self.assertEqual(transfer.out_leg.due_date, date(2026, 2, 1))
        self.assertEqual(transfer.in_leg.due_date, date(2026, 2, 1))

    def test_unexecuted_without_due_date_raises(self):
        with self.assertRaises(ValidationError):
            services.create_transfer(from_account=self.checking, to_account=self.savings,
                                      amount=Decimal('100.00'), description='move')


class TransferCreateFormDueDateTests(TestCase):
    def setUp(self):
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))

    def test_executed_without_due_date_is_valid(self):
        form = TransferCreateForm(data={
            'from_account': self.checking.pk, 'to_account': self.savings.pk, 'amount': '50.00',
            'description': 'move', 'executed': True, 'executed_date': '2026-02-01',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['due_date'], date(2026, 2, 1))

    def test_unexecuted_without_due_date_is_invalid(self):
        form = TransferCreateForm(data={
            'from_account': self.checking.pk, 'to_account': self.savings.pk, 'amount': '50.00',
            'description': 'move',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('due_date', form.errors)
