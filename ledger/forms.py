from django import forms
from django.utils import timezone

from .models import Account, Category, RecurringTransaction, Transaction


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ['name', 'account_type', 'current_balance', 'is_active']


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description', 'typical_direction']


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


class TransactionCreateForm(ActiveAccountFieldMixin, forms.ModelForm):
    executed = forms.BooleanField(
        required=False, label='Already executed?',
        help_text='Check this if logging something that already happened.',
    )
    executed_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    class Meta:
        model = Transaction
        fields = ['account', 'category', 'direction', 'amount', 'description', 'due_date']
        widgets = {'due_date': forms.DateInput(attrs={'type': 'date'})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('executed') and not cleaned.get('executed_date'):
            cleaned['executed_date'] = timezone.localdate()
        if not cleaned.get('executed'):
            cleaned['executed_date'] = None
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


class ProjectionForm(forms.Form):
    target_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    account = forms.ModelChoiceField(
        queryset=Account.objects.filter(is_active=True), required=False,
        empty_label='All accounts (combined)',
    )
