from datetime import date
from decimal import Decimal

from django.test import TestCase

from ledger.forms import RecurringTransactionForm, TransactionEditForm
from ledger.models import Account, RecurringTransaction, Transaction


class ActiveAccountFieldMixinRegressionTests(TestCase):
    """Regression coverage for a real bug: ActiveAccountFieldMixin restricted
    the `account` field to is_active=True accounts unconditionally, including
    when editing an EXISTING Transaction/RecurringTransaction whose account
    was deactivated after the fact. That made the account field render with no
    valid selected option and any save fail validation — blocking even
    non-financial edits (due date, description) with no way to fix it short of
    reactivating the account. The fix keeps the instance's current account
    selectable on edit while still restricting NEW selections to active ones."""

    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_editing_transaction_with_deactivated_account_is_still_possible(self):
        txn = Transaction.objects.create(
            account=self.account, direction=Transaction.Direction.OUT,
            amount=Decimal('10.00'), due_date=date(2026, 1, 1),
        )
        self.account.is_active = False
        self.account.save()

        form = TransactionEditForm(instance=txn, data={
            'account': self.account.pk, 'category': '', 'direction': Transaction.Direction.OUT,
            'amount': '10.00', 'description': 'fixed typo', 'due_date': '2026-01-02',
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_new_transaction_cannot_select_a_deactivated_account(self):
        self.account.is_active = False
        self.account.save()
        form = TransactionEditForm(data={
            'account': self.account.pk, 'category': '', 'direction': Transaction.Direction.OUT,
            'amount': '10.00', 'description': 'new', 'due_date': '2026-01-02',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('account', form.errors)

    def test_editing_recurring_template_with_deactivated_account_is_still_possible(self):
        recurring = RecurringTransaction.objects.create(
            account=self.account, direction=RecurringTransaction.Direction.OUT,
            amount=Decimal('20.00'), description='Netflix',
            frequency=RecurringTransaction.Frequency.MONTHLY, interval=1, start_date=date(2026, 1, 1),
        )
        self.account.is_active = False
        self.account.save()

        form = RecurringTransactionForm(instance=recurring, data={
            'account': self.account.pk, 'category': '', 'direction': RecurringTransaction.Direction.OUT,
            'amount': '25.00', 'description': 'Netflix (price change)', 'frequency': 'monthly',
            'interval': '1', 'start_date': '2026-01-01', 'end_date': '', 'is_active': 'on',
        })
        self.assertTrue(form.is_valid(), form.errors)
