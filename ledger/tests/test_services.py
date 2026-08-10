from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from ledger import services
from ledger.models import Account, Transaction


class ExecuteUnexecuteTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def make_txn(self, direction, amount):
        return Transaction.objects.create(
            account=self.account, direction=direction, amount=Decimal(amount), due_date='2026-01-01',
        )

    def test_execute_out_subtracts_from_balance(self):
        txn = self.make_txn(Transaction.Direction.OUT, '50.00')
        services.execute_transaction(txn)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('950.00'))
        self.assertTrue(txn.executed)
        self.assertIsNotNone(txn.executed_date)

    def test_execute_in_adds_to_balance(self):
        txn = self.make_txn(Transaction.Direction.IN, '200.00')
        services.execute_transaction(txn)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('1200.00'))

    def test_unexecute_is_exact_inverse(self):
        txn = self.make_txn(Transaction.Direction.OUT, '75.00')
        services.execute_transaction(txn)
        services.unexecute_transaction(txn)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('1000.00'))
        self.assertFalse(txn.executed)
        self.assertIsNone(txn.executed_date)

    def test_unexecuted_transaction_does_not_affect_balance(self):
        self.make_txn(Transaction.Direction.OUT, '999.00')
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('1000.00'))

    def test_double_execute_raises(self):
        txn = self.make_txn(Transaction.Direction.OUT, '10.00')
        services.execute_transaction(txn)
        with self.assertRaises(ValidationError):
            services.execute_transaction(txn)

    def test_unexecute_when_not_executed_raises(self):
        txn = self.make_txn(Transaction.Direction.OUT, '10.00')
        with self.assertRaises(ValidationError):
            services.unexecute_transaction(txn)

    def test_delete_executed_transaction_raises(self):
        txn = self.make_txn(Transaction.Direction.OUT, '10.00')
        services.execute_transaction(txn)
        with self.assertRaises(ValidationError):
            services.delete_transaction(txn)
        self.assertTrue(Transaction.objects.filter(pk=txn.pk).exists())

    def test_delete_unexecuted_transaction_succeeds(self):
        txn = self.make_txn(Transaction.Direction.OUT, '10.00')
        services.delete_transaction(txn)
        self.assertFalse(Transaction.objects.filter(pk=txn.pk).exists())

    def test_update_full_raises_when_executed(self):
        txn = self.make_txn(Transaction.Direction.OUT, '10.00')
        services.execute_transaction(txn)
        with self.assertRaises(ValidationError):
            services.update_transaction_full(
                txn, account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('20.00'), description='changed', due_date='2026-02-01',
            )

    def test_update_open_fields_works_regardless_of_executed_state(self):
        txn = self.make_txn(Transaction.Direction.OUT, '10.00')
        services.execute_transaction(txn)
        services.update_transaction_open_fields(txn, category=None, description='updated', due_date='2026-03-01')
        self.account.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(txn.description, 'updated')
        self.assertEqual(self.account.current_balance, Decimal('990.00'))


class StaleObjectRaceRegressionTests(TestCase):
    """Regression coverage for a real bug: execute_transaction/unexecute_transaction/
    update_transaction_full/delete_transaction used to check `txn.executed` against
    whatever the CALLER's in-memory object happened to hold, not the current DB
    state. Loading the same row into two separate objects (two tabs, a double
    form submit, a background job) and acting on the stale one bypassed every
    guard, producing double-applied balance deltas or orphaned adjustments. The
    fix re-fetches the Transaction under select_for_update() before checking
    `executed`, so these now correctly reject the stale caller instead of
    silently trusting it."""

    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.txn = Transaction.objects.create(
            account=self.account, direction=Transaction.Direction.OUT,
            amount=Decimal('100.00'), due_date='2026-01-01',
        )

    def load_stale_copy(self):
        return Transaction.objects.get(pk=self.txn.pk)

    def test_double_execute_via_two_stale_objects_is_rejected(self):
        fresh = self.load_stale_copy()
        stale = self.load_stale_copy()

        services.execute_transaction(fresh)
        with self.assertRaises(ValidationError):
            services.execute_transaction(stale)

        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('900.00'))

    def test_double_unexecute_via_two_stale_objects_is_rejected(self):
        services.execute_transaction(self.txn)
        fresh = self.load_stale_copy()
        stale = self.load_stale_copy()

        services.unexecute_transaction(fresh)
        with self.assertRaises(ValidationError):
            services.unexecute_transaction(stale)

        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('1000.00'))

    def test_update_full_on_stale_object_after_concurrent_execute_is_rejected(self):
        stale = self.load_stale_copy()
        services.execute_transaction(self.txn)

        with self.assertRaises(ValidationError):
            services.update_transaction_full(
                stale, account=self.account, category=None, direction=Transaction.Direction.OUT,
                amount=Decimal('500.00'), description='changed', due_date='2026-02-01',
            )

        self.txn.refresh_from_db()
        self.account.refresh_from_db()
        self.assertTrue(self.txn.executed)
        self.assertEqual(self.txn.amount, Decimal('100.00'))
        self.assertEqual(self.account.current_balance, Decimal('900.00'))

    def test_delete_stale_object_after_concurrent_execute_is_rejected(self):
        stale = self.load_stale_copy()
        services.execute_transaction(self.txn)

        with self.assertRaises(ValidationError):
            services.delete_transaction(stale)

        self.account.refresh_from_db()
        self.assertTrue(Transaction.objects.filter(pk=self.txn.pk).exists())
        self.assertEqual(self.account.current_balance, Decimal('900.00'))
