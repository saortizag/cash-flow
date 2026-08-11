from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from ledger import services
from ledger.forms import TransferCreateForm
from ledger.models import Account, Transaction, Transfer

User = get_user_model()


class TransferLifecycleTests(TestCase):
    def setUp(self):
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('500.00'))

    def test_create_transfer_creates_two_linked_legs(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        self.assertEqual(transfer.out_leg.account, self.checking)
        self.assertEqual(transfer.out_leg.direction, Transaction.Direction.OUT)
        self.assertEqual(transfer.in_leg.account, self.savings)
        self.assertEqual(transfer.in_leg.direction, Transaction.Direction.IN)
        self.assertEqual(transfer.out_leg.amount, transfer.in_leg.amount)
        self.assertFalse(transfer.out_leg.executed)

    def test_execute_transfer_moves_both_balances(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        services.execute_transfer(transfer)
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('900.00'))
        self.assertEqual(self.savings.current_balance, Decimal('600.00'))

    def test_unexecute_transfer_reverses_both_balances(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1),
                                             executed=True)
        services.unexecute_transfer(transfer)
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('1000.00'))
        self.assertEqual(self.savings.current_balance, Decimal('500.00'))

    def test_execute_raises_without_source_account(self):
        transfer = services.create_transfer(from_account=None, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            services.execute_transfer(transfer)
        self.savings.refresh_from_db()
        self.assertEqual(self.savings.current_balance, Decimal('500.00'))

    def test_execute_is_all_or_nothing(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        # Pre-execute just the in_leg directly, simulating a state where the
        # second leg of execute_transfer's pair will legitimately fail.
        services.execute_transaction(transfer.in_leg)

        with self.assertRaises(ValidationError):
            services.execute_transfer(transfer)

        transfer.out_leg.refresh_from_db()
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        # out_leg must NOT have been left executed even though it "succeeded"
        # before the in_leg raised — the outer atomic rolls back both.
        self.assertFalse(transfer.out_leg.executed)
        self.assertEqual(self.checking.current_balance, Decimal('1000.00'))
        self.assertEqual(self.savings.current_balance, Decimal('600.00'))  # only the pre-execute's effect

    def test_update_transfer_edits_both_legs_in_lockstep(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        services.update_transfer(transfer, amount=Decimal('150.00'), description='updated', due_date=date(2026, 2, 1))
        transfer.out_leg.refresh_from_db()
        transfer.in_leg.refresh_from_db()
        for leg in (transfer.out_leg, transfer.in_leg):
            self.assertEqual(leg.amount, Decimal('150.00'))
            self.assertEqual(leg.description, 'updated')
            self.assertEqual(leg.due_date, date(2026, 2, 1))

    def test_update_transfer_raises_if_executed(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1),
                                             executed=True)
        with self.assertRaises(ValidationError):
            services.update_transfer(transfer, amount=Decimal('999.00'), description='x', due_date=date(2026, 1, 1))

    def test_delete_transfer_removes_both_legs(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        out_pk, in_pk = transfer.out_leg_id, transfer.in_leg_id
        services.delete_transfer(transfer)
        self.assertFalse(Transaction.objects.filter(pk__in=[out_pk, in_pk]).exists())
        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_delete_transfer_raises_if_executed(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1),
                                             executed=True)
        with self.assertRaises(ValidationError):
            services.delete_transfer(transfer)
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_assign_account_to_unassigned_out_leg(self):
        transfer = services.create_transfer(from_account=None, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        services.assign_account(transfer.out_leg, self.checking)
        transfer.out_leg.refresh_from_db()
        self.assertEqual(transfer.out_leg.account, self.checking)
        services.execute_transfer(transfer)
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('900.00'))
        self.assertEqual(self.savings.current_balance, Decimal('600.00'))

    def test_assign_account_raises_if_already_assigned(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move', due_date=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            services.assign_account(transfer.out_leg, self.savings)


class TransferFormTests(TestCase):
    def setUp(self):
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_same_account_for_source_and_destination_is_invalid(self):
        form = TransferCreateForm(data={
            'from_account': self.checking.pk, 'to_account': self.checking.pk, 'amount': '10.00',
            'description': '', 'due_date': '2026-01-01',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('to_account', form.errors)


class TransferLegViewGuardTests(TestCase):
    """Regression coverage for the guard added to the plain transaction_*
    views: a transfer leg must never be individually executed/edited/deleted
    outside the transfer-specific flow, since that would break the paired
    'equal and opposite' invariant."""

    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('500.00'))
        self.transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                                   amount=Decimal('100.00'), description='move',
                                                   due_date=date(2026, 1, 1))

    def test_transaction_update_redirects_to_transfer(self):
        response = self.client.get(reverse('ledger:transaction_update', args=[self.transfer.out_leg_id]))
        self.assertRedirects(response, reverse('ledger:transfer_detail', args=[self.transfer.pk]))

    def test_transaction_execute_redirects_to_transfer(self):
        response = self.client.get(reverse('ledger:transaction_execute', args=[self.transfer.out_leg_id]))
        self.assertRedirects(response, reverse('ledger:transfer_detail', args=[self.transfer.pk]))

    def test_transaction_delete_redirects_to_transfer(self):
        response = self.client.get(reverse('ledger:transaction_delete', args=[self.transfer.out_leg_id]))
        self.assertRedirects(response, reverse('ledger:transfer_detail', args=[self.transfer.pk]))

    def test_transaction_unexecute_redirects_to_transfer(self):
        services.execute_transfer(self.transfer)
        response = self.client.post(reverse('ledger:transaction_unexecute', args=[self.transfer.in_leg_id]))
        self.assertRedirects(response, reverse('ledger:transfer_detail', args=[self.transfer.pk]))
        self.transfer.in_leg.refresh_from_db()
        self.assertTrue(self.transfer.in_leg.executed)  # untouched by the blocked direct call
