import os
import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()

# Real FileSystemStorage writes to disk on every test that touches attachment= — redirect
# MEDIA_ROOT to a throwaway directory for this module rather than polluting the real media/.
TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='cash_test_media_')


def tearDownModule():
    shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)


def make_file(name='receipt.pdf', content=b'%PDF-1.4 fake pdf content', content_type='application/pdf'):
    return SimpleUploadedFile(name, content, content_type=content_type)


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AttachmentServiceTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_create_transaction_with_attachment(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='Groceries', due_date=date(2026, 1, 1),
            attachment=make_file(),
        )
        self.assertTrue(txn.attachment)
        self.assertIn('receipt', txn.attachment.name)

    def test_update_transaction_attachment_on_executed_transaction_leaves_balance_untouched(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='Groceries', due_date=date(2026, 1, 1), executed=True,
        )
        services.update_transaction_attachment(txn, make_file())
        txn.refresh_from_db()
        self.account.refresh_from_db()
        self.assertTrue(txn.attachment)
        self.assertTrue(txn.executed)
        self.assertEqual(self.account.current_balance, Decimal('950.00'))

    def test_clear_transaction_attachment(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='x', due_date=date(2026, 1, 1), attachment=make_file(),
        )
        services.update_transaction_attachment(txn, None)
        txn.refresh_from_db()
        self.assertFalse(txn.attachment)

    def test_create_transfer_with_attachment_is_stored_on_out_leg_only(self):
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        transfer = services.create_transfer(from_account=self.account, to_account=savings,
                                             amount=Decimal('100.00'), description='move',
                                             due_date=date(2026, 1, 1), attachment=make_file())
        self.assertTrue(transfer.out_leg.attachment)
        self.assertFalse(transfer.in_leg.attachment)
        self.assertEqual(transfer.attachment, transfer.out_leg.attachment)

    def test_update_transfer_attachment_works_even_when_executed(self):
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        transfer = services.create_transfer(from_account=self.account, to_account=savings,
                                             amount=Decimal('100.00'), description='move',
                                             due_date=date(2026, 1, 1), executed=True)
        # update_transfer itself raises here (financial fields lock once executed) —
        # update_transfer_attachment must NOT share that restriction.
        with self.assertRaises(ValidationError):
            services.update_transfer(transfer, amount=Decimal('1.00'), description='x', due_date=date(2026, 1, 1))
        services.update_transfer_attachment(transfer, make_file())
        transfer.out_leg.refresh_from_db()
        self.assertTrue(transfer.out_leg.attachment)

    # FileField doesn't delete the underlying file from storage on its own — these four cover
    # services.py's explicit cleanup (via transaction.on_commit, so a rolled-back request never
    # deletes a file a surviving row still points at). captureOnCommitCallbacks(execute=True) is
    # needed because TestCase itself wraps each test in a transaction that's rolled back, which
    # would otherwise make on_commit callbacks never fire during the test.

    def test_replacing_attachment_deletes_old_file_from_storage(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='x', due_date=date(2026, 1, 1), attachment=make_file(),
        )
        old_path = txn.attachment.path
        self.assertTrue(os.path.exists(old_path))
        with self.captureOnCommitCallbacks(execute=True):
            services.update_transaction_attachment(txn, make_file('new.pdf'))
        self.assertFalse(os.path.exists(old_path))

    def test_clearing_attachment_deletes_file_from_storage(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='x', due_date=date(2026, 1, 1), attachment=make_file(),
        )
        old_path = txn.attachment.path
        with self.captureOnCommitCallbacks(execute=True):
            services.update_transaction_attachment(txn, None)
        self.assertFalse(os.path.exists(old_path))

    def test_deleting_transaction_deletes_attachment_from_storage(self):
        txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('50.00'), description='x', due_date=date(2026, 1, 1), attachment=make_file(),
        )
        path = txn.attachment.path
        with self.captureOnCommitCallbacks(execute=True):
            services.delete_transaction(txn)
        self.assertFalse(os.path.exists(path))

    def test_deleting_transfer_deletes_attachment_from_storage(self):
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        transfer = services.create_transfer(from_account=self.account, to_account=savings,
                                             amount=Decimal('100.00'), description='move',
                                             due_date=date(2026, 1, 1), attachment=make_file())
        path = transfer.out_leg.attachment.path
        with self.captureOnCommitCallbacks(execute=True):
            services.delete_transfer(transfer)
        self.assertFalse(os.path.exists(path))


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AttachmentValidationTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def make_txn(self, **file_kwargs):
        return Transaction(account=self.account, direction=Transaction.Direction.OUT, amount=Decimal('1.00'),
                            due_date=date(2026, 1, 1), attachment=make_file(**file_kwargs))

    def test_disallowed_extension_rejected_on_full_clean(self):
        txn = self.make_txn(name='malware.exe', content_type='application/octet-stream')
        with self.assertRaises(ValidationError):
            txn.full_clean()

    def test_oversized_file_rejected_on_full_clean(self):
        txn = self.make_txn(name='big.pdf', content=b'0' * (11 * 1024 * 1024))  # 11MB > 10MB limit
        with self.assertRaises(ValidationError):
            txn.full_clean()

    def test_allowed_extension_and_size_pass(self):
        self.make_txn(name='receipt.pdf').full_clean()  # should not raise


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AttachmentViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_login(self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.txn = services.create_transaction(
            account=self.account, category=None, direction=Transaction.Direction.OUT,
            amount=Decimal('10.00'), description='x', due_date=date(2026, 1, 1))

    def test_download_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('ledger:transaction_attachment_download', args=[self.txn.pk]))
        self.assertEqual(response.status_code, 302)

    def test_download_404_without_attachment(self):
        response = self.client.get(reverse('ledger:transaction_attachment_download', args=[self.txn.pk]))
        self.assertEqual(response.status_code, 404)

    def test_upload_then_download(self):
        response = self.client.post(reverse('ledger:transaction_attachment', args=[self.txn.pk]),
                                     {'attachment': make_file()})
        self.assertRedirects(response, reverse('ledger:transaction_list'))
        self.txn.refresh_from_db()
        self.assertTrue(self.txn.attachment)
        response = self.client.get(reverse('ledger:transaction_attachment_download', args=[self.txn.pk]))
        self.assertEqual(response.status_code, 200)

    def test_clear_via_checkbox(self):
        services.update_transaction_attachment(self.txn, make_file())
        response = self.client.post(reverse('ledger:transaction_attachment', args=[self.txn.pk]),
                                     {'attachment-clear': 'on'})
        self.assertRedirects(response, reverse('ledger:transaction_list'))
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.attachment)

    def test_no_op_submit_leaves_existing_attachment_untouched(self):
        services.update_transaction_attachment(self.txn, make_file())
        original_name = self.txn.attachment.name
        response = self.client.post(reverse('ledger:transaction_attachment', args=[self.txn.pk]), {})
        self.assertRedirects(response, reverse('ledger:transaction_list'))
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.attachment.name, original_name)

    def test_disallowed_extension_shows_form_error_not_500(self):
        response = self.client.post(reverse('ledger:transaction_attachment', args=[self.txn.pk]),
                                     {'attachment': make_file('bad.exe', content_type='application/octet-stream')})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['form'].is_valid())

    def test_attachment_on_transfer_leg_redirects_to_transfer_attachment(self):
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        transfer = services.create_transfer(from_account=self.account, to_account=savings,
                                             amount=Decimal('50.00'), description='move', due_date=date(2026, 1, 1))
        response = self.client.get(reverse('ledger:transaction_attachment', args=[transfer.out_leg_id]))
        self.assertRedirects(response, reverse('ledger:transfer_attachment', args=[transfer.pk]))

    def test_transfer_attachment_upload_works_when_executed(self):
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        transfer = services.create_transfer(from_account=self.account, to_account=savings,
                                             amount=Decimal('50.00'), description='move', due_date=date(2026, 1, 1),
                                             executed=True)
        response = self.client.post(reverse('ledger:transfer_attachment', args=[transfer.pk]),
                                     {'attachment': make_file()})
        self.assertRedirects(response, reverse('ledger:transfer_detail', args=[transfer.pk]))
        transfer.out_leg.refresh_from_db()
        self.assertTrue(transfer.out_leg.attachment)
