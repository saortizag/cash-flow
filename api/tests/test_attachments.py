import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ledger import services
from ledger.models import Account, Transaction

User = get_user_model()

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='cash_api_test_media_')


def tearDownModule():
    shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)


def make_file(name='receipt.pdf', content=b'%PDF-1.4 fake pdf content', content_type='application/pdf'):
    return SimpleUploadedFile(name, content, content_type=content_type)


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class TransactionAttachmentTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.account = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))

    def test_create_with_attachment_exposes_url_and_name_not_raw_field(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'account': self.account.pk, 'direction': Transaction.Direction.OUT,
            'amount': '50.00', 'description': 'Groceries', 'due_date': '2026-08-20',
            'attachment': make_file('receipt.pdf'),
        }, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn('attachment', response.data)
        self.assertEqual(response.data['attachment_name'], 'receipt.pdf')
        self.assertIsNotNone(response.data['attachment_url'])

    def test_attachment_url_downloads_the_file(self):
        response = self.client.post(reverse('api:transaction-list'), {
            'account': self.account.pk, 'direction': Transaction.Direction.OUT,
            'amount': '50.00', 'description': 'x', 'due_date': '2026-08-20',
            'attachment': make_file(),
        }, format='multipart')
        download = self.client.get(response.data['attachment_url'])
        self.assertEqual(download.status_code, status.HTTP_200_OK)

    def test_download_404_without_attachment(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20))
        response = self.client.get(reverse('api:transaction-attachment', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_attachment_action_requires_authentication(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20))
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('api:transaction-attachment', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_set_attachment_via_action(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20))
        response = self.client.post(reverse('api:transaction-attachment', args=[txn.pk]),
                                     {'attachment': make_file('invoice.pdf')}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['attachment_name'], 'invoice.pdf')

    def test_set_attachment_action_works_on_executed_transaction(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20), executed=True)
        response = self.client.post(reverse('api:transaction-attachment', args=[txn.pk]),
                                     {'attachment': make_file()}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('990.00'))  # untouched by the attach

    def test_clear_attachment_via_delete(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20), attachment=make_file())
        response = self.client.delete(reverse('api:transaction-attachment', args=[txn.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        txn.refresh_from_db()
        self.assertFalse(txn.attachment)

    def test_disallowed_extension_returns_400(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20))
        response = self.client.post(
            reverse('api:transaction-attachment', args=[txn.pk]),
            {'attachment': make_file('malware.exe', content_type='application/octet-stream')},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_oversized_file_returns_400(self):
        txn = services.create_transaction(account=self.account, category=None,
                                           direction=Transaction.Direction.OUT, amount=Decimal('10.00'),
                                           description='x', due_date=date(2026, 8, 20))
        big = make_file('big.pdf', content=b'0' * (11 * 1024 * 1024))
        response = self.client.post(reverse('api:transaction-attachment', args=[txn.pk]),
                                     {'attachment': big}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_post_and_delete_blocked_on_transfer_leg_but_get_allowed(self):
        savings = Account.objects.create(name='Savings', current_balance=Decimal('0.00'))
        transfer = services.create_transfer(from_account=self.account, to_account=savings,
                                             amount=Decimal('50.00'), description='move',
                                             due_date=date(2026, 8, 20), attachment=make_file())
        post_response = self.client.post(reverse('api:transaction-attachment', args=[transfer.out_leg_id]),
                                          {'attachment': make_file()}, format='multipart')
        self.assertEqual(post_response.status_code, status.HTTP_400_BAD_REQUEST)
        delete_response = self.client.delete(reverse('api:transaction-attachment', args=[transfer.out_leg_id]))
        self.assertEqual(delete_response.status_code, status.HTTP_400_BAD_REQUEST)
        get_response = self.client.get(reverse('api:transaction-attachment', args=[transfer.out_leg_id]))
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class TransferAttachmentTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.checking = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        self.savings = Account.objects.create(name='Savings', current_balance=Decimal('500.00'))

    def test_create_transfer_with_attachment(self):
        response = self.client.post(reverse('api:transfer-list'), {
            'from_account': self.checking.pk, 'to_account': self.savings.pk,
            'amount': '100.00', 'description': 'move', 'due_date': '2026-08-20',
            'attachment': make_file('proof.pdf'),
        }, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn('attachment', response.data)
        self.assertEqual(response.data['attachment_name'], 'proof.pdf')

    def test_set_attachment_action_works_when_executed(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move',
                                             due_date=date(2026, 8, 20), executed=True)
        response = self.client.post(reverse('api:transfer-attachment', args=[transfer.pk]),
                                     {'attachment': make_file()}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal('900.00'))  # untouched by the attach
        self.assertEqual(self.savings.current_balance, Decimal('600.00'))

    def test_clear_attachment_via_delete(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move',
                                             due_date=date(2026, 8, 20), attachment=make_file())
        response = self.client.delete(reverse('api:transfer-attachment', args=[transfer.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        transfer.out_leg.refresh_from_db()
        self.assertFalse(transfer.out_leg.attachment)

    def test_download_404_without_attachment(self):
        transfer = services.create_transfer(from_account=self.checking, to_account=self.savings,
                                             amount=Decimal('100.00'), description='move',
                                             due_date=date(2026, 8, 20))
        response = self.client.get(reverse('api:transfer-attachment', args=[transfer.pk]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
