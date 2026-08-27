from django import forms
from django.core.validators import FileExtensionValidator
from django.utils import timezone

from .models import (
    ATTACHMENT_ALLOWED_EXTENSIONS,
    Account,
    Category,
    RecurringTransaction,
    Transaction,
    validate_attachment_size,
)

ATTACHMENT_VALIDATORS = [
    FileExtensionValidator(allowed_extensions=ATTACHMENT_ALLOWED_EXTENSIONS),
    validate_attachment_size,
]


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ['name', 'account_type', 'current_balance', 'is_active', 'cut_day', 'payment_due_day']
        help_texts = {
            'current_balance': 'For a credit card, enter this as a NEGATIVE number (e.g. -450.00 means you owe 450).',
        }


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description', 'typical_direction']


class CreditCardBootstrapForm(forms.Form):
    """'I owe X, due on D' — onboarding a card that already has a balance,
    with no itemized purchase history to back it. See services.bootstrap_statement."""
    account = forms.ModelChoiceField(
        queryset=Account.objects.filter(is_active=True, account_type=Account.AccountType.CREDIT_CARD),
        label='Credit card',
    )
    amount_owed = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    due_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))


class ActiveAccountFieldMixin:
    """Restricts the `account` ModelChoiceField to active accounts — you can't
    point NEW activity at an account that's been archived. When editing an
    EXISTING row, its current account stays a valid choice even if it was
    deactivated since — otherwise archiving an account would make every
    transaction/template that already points at it uneditable (the account
    field would have no valid selected option and every save would fail
    validation), blocking even non-financial edits like fixing a due date."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Account.objects.filter(is_active=True)
        instance = getattr(self, 'instance', None)
        if instance and instance.pk and instance.account_id:
            queryset = queryset | Account.objects.filter(pk=instance.account_id)
        self.fields['account'].queryset = queryset
        # Transaction.account is nullable (a not-yet-assigned planned payment);
        # RecurringTransaction.account is not, so this only changes anything
        # for the forms where the underlying model field is actually optional.
        if not self.fields['account'].required:
            self.fields['account'].empty_label = 'Not yet assigned'


class TransactionCreateForm(ActiveAccountFieldMixin, forms.ModelForm):
    executed = forms.BooleanField(
        required=False, label='Already executed?',
        help_text='Check this if logging something that already happened.',
    )
    executed_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    class Meta:
        model = Transaction
        fields = ['account', 'category', 'direction', 'amount', 'description', 'due_date', 'attachment']
        widgets = {'due_date': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Not required at the field level — clean() below fills it in from executed_date when
        # the transaction is already executed, so retroactively logging something that already
        # happened doesn't also require separately typing the same date into "due date".
        self.fields['due_date'].required = False

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('executed') and not cleaned.get('executed_date'):
            cleaned['executed_date'] = timezone.localdate()
        if not cleaned.get('executed'):
            cleaned['executed_date'] = None
        if cleaned.get('executed') and not cleaned.get('due_date'):
            cleaned['due_date'] = cleaned.get('executed_date')
        if not cleaned.get('executed') and not cleaned.get('due_date'):
            self.add_error('due_date', 'This field is required.')
        if cleaned.get('executed') and not cleaned.get('account'):
            self.add_error('account', 'Assign an account before marking this as already executed.')
        return cleaned


class TransactionEditForm(ActiveAccountFieldMixin, forms.ModelForm):
    """Only ever instantiated by the view when transaction.executed is False."""

    class Meta:
        model = Transaction
        fields = ['account', 'category', 'direction', 'amount', 'description', 'due_date']
        widgets = {'due_date': forms.DateInput(attrs={'type': 'date'})}


class TransactionOpenFieldsForm(forms.ModelForm):
    """Only ever instantiated by the view when transaction.executed is True.
    Deliberately excludes account/direction/amount so an executed transaction's
    financial fields can never be edited without un-executing first."""

    class Meta:
        model = Transaction
        fields = ['category', 'description', 'due_date']
        widgets = {'due_date': forms.DateInput(attrs={'type': 'date'})}


class AssignAccountForm(forms.Form):
    """Only ever shown when the target Transaction's account is currently None."""
    account = forms.ModelChoiceField(queryset=Account.objects.filter(is_active=True))


class ExecuteTransactionForm(forms.Form):
    executed_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), initial=timezone.localdate)


class RecurringTransactionForm(ActiveAccountFieldMixin, forms.ModelForm):
    class Meta:
        model = RecurringTransaction
        fields = ['account', 'category', 'direction', 'amount', 'description',
                  'frequency', 'interval', 'start_date', 'end_date', 'is_active']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }


class TransferCreateForm(forms.Form):
    from_account = forms.ModelChoiceField(
        queryset=Account.objects.filter(is_active=True), required=False, empty_label='Not yet assigned',
        label='From',
    )
    to_account = forms.ModelChoiceField(queryset=Account.objects.filter(is_active=True), label='To')
    amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    description = forms.CharField(max_length=255, required=False)
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    executed = forms.BooleanField(
        required=False, label='Already executed?',
        help_text='Check this if logging a transfer that already happened.',
    )
    executed_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    attachment = forms.FileField(required=False, validators=ATTACHMENT_VALIDATORS)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('executed') and not cleaned.get('executed_date'):
            cleaned['executed_date'] = timezone.localdate()
        if not cleaned.get('executed'):
            cleaned['executed_date'] = None
        if cleaned.get('executed') and not cleaned.get('due_date'):
            cleaned['due_date'] = cleaned.get('executed_date')
        if not cleaned.get('executed') and not cleaned.get('due_date'):
            self.add_error('due_date', 'This field is required.')
        if cleaned.get('executed') and not cleaned.get('from_account'):
            self.add_error('from_account', 'Assign a source account before marking this as already executed.')
        from_account, to_account = cleaned.get('from_account'), cleaned.get('to_account')
        if from_account and to_account and from_account.pk == to_account.pk:
            self.add_error('to_account', 'Source and destination must be different accounts.')
        return cleaned


class TransferEditForm(forms.Form):
    """Amount/description/due_date only — mirrors services.update_transfer.
    Only ever shown when the transfer is unexecuted."""
    amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    description = forms.CharField(max_length=255, required=False)
    due_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))


class TransferExecuteForm(forms.Form):
    executed_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), initial=timezone.localdate)


class AttachmentForm(forms.Form):
    """Set/replace/clear the support file on an existing Transaction or Transfer — shared by
    ledger.views.transaction_attachment and transfer_attachment. Deliberately a plain Form (not
    tied to a model instance) so it works identically for both; the view passes the current
    file in as `initial` so ClearableFileInput can render its own "clear" checkbox. On submit,
    cleaned_data['attachment'] is None (nothing changed), False (clear checkbox ticked), or an
    uploaded file (replace) — standard Django FileField semantics, resolved by the view before
    calling services.update_transaction_attachment / update_transfer_attachment."""
    attachment = forms.FileField(required=False, widget=forms.ClearableFileInput, validators=ATTACHMENT_VALIDATORS)


class ProjectionForm(forms.Form):
    target_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    account = forms.ModelChoiceField(
        queryset=Account.objects.filter(is_active=True), required=False,
        empty_label='All accounts (combined)',
    )
