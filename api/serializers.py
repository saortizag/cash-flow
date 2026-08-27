"""
Serializers call straight into ledger.services for anything that mutates a
balance or an executed flag — see ledger/services.py's module docstring for
why that module is the only sanctioned path. Nothing here talks to
Account.current_balance or Transaction.executed directly.
"""

import os
from decimal import Decimal

from django.core.validators import FileExtensionValidator
from django.urls import reverse
from rest_framework import serializers

from ledger import services
from ledger.models import (
    ATTACHMENT_ALLOWED_EXTENSIONS,
    Account,
    Category,
    CreditCardStatement,
    RecurringTransaction,
    Transaction,
    Transfer,
    validate_attachment_size,
)

ATTACHMENT_VALIDATORS = [
    FileExtensionValidator(allowed_extensions=ATTACHMENT_ALLOWED_EXTENSIONS),
    validate_attachment_size,
]


def _attachment_url(context, field_file, url_name, pk):
    """Never expose the raw storage URL (attachment.url) — nothing serves /media/ publicly, by
    design (see cash.settings' MEDIA_ROOT comment). Points at the authenticated download action
    instead, absolute if a request is available in context (it always is via a ViewSet's
    get_serializer_context(), but a nested/manually-built serializer might omit it)."""
    if not field_file:
        return None
    request = context.get('request')
    url = reverse(url_name, args=[pk])
    return request.build_absolute_uri(url) if request else url


def _attachment_name(field_file):
    return os.path.basename(field_file.name) if field_file else None


class ActiveAccountFieldMixin:
    """Restricts the `account` field's queryset to active accounts, OR'd with
    the instance's current account if it's since been deactivated — mirrors
    ledger.forms.ActiveAccountFieldMixin exactly, so an existing row pointed
    at a now-archived account can still be edited (just not newly assigned to
    another archived one)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Account.objects.filter(is_active=True)
        instance = self.instance
        if instance and getattr(instance, 'pk', None) and instance.account_id:
            queryset = queryset | Account.objects.filter(pk=instance.account_id)
        self.fields['account'].queryset = queryset


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = [
            'id', 'name', 'account_type', 'current_balance', 'is_active',
            'cut_day', 'payment_due_day', 'next_statement_cut_date',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['next_statement_cut_date', 'created_at', 'updated_at']


class CreditCardBootstrapSerializer(serializers.Serializer):
    """'I owe X, due D' for a card with no itemized history yet. Mirrors
    ledger.forms.CreditCardBootstrapForm minus the `account` field, which
    comes from the URL instead. Maps to services.bootstrap_statement."""
    amount_owed = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.00'))
    due_date = serializers.DateField()


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'description', 'typical_direction']


class TransactionSerializer(ActiveAccountFieldMixin, serializers.ModelSerializer):
    """Read + update. `executed`/`executed_date` are read-only here —
    flipping them goes through the dedicated execute/unexecute actions,
    matching ledger.forms.TransactionEditForm (which doesn't expose them at
    all). Full account/direction/amount edits are only accepted while the
    transaction is unexecuted; see validate()/update()."""

    is_transfer_leg = serializers.SerializerMethodField()
    attachment_url = serializers.SerializerMethodField()
    attachment_name = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = [
            'id', 'account', 'category', 'direction', 'amount', 'description',
            'due_date', 'executed', 'executed_date', 'recurring_source', 'statement',
            'is_transfer_leg', 'attachment_url', 'attachment_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'executed', 'executed_date', 'recurring_source', 'statement',
            'created_at', 'updated_at',
        ]

    def get_is_transfer_leg(self, obj):
        return bool(getattr(obj, 'transfer_as_source', None) or getattr(obj, 'transfer_as_destination', None))

    def get_attachment_url(self, obj):
        return _attachment_url(self.context, obj.attachment, 'api:transaction-attachment', obj.pk)

    def get_attachment_name(self, obj):
        return _attachment_name(obj.attachment)

    def validate(self, attrs):
        if self.instance is not None and self.instance.executed:
            locked = {'account', 'direction', 'amount'} & set(attrs)
            if locked:
                raise serializers.ValidationError({
                    field: 'Cannot change this field on an executed transaction. Un-execute it first.'
                    for field in locked
                })
        return attrs

    def update(self, instance, validated_data):
        if instance.executed:
            return services.update_transaction_open_fields(
                instance,
                category=validated_data.get('category', instance.category),
                description=validated_data.get('description', instance.description),
                due_date=validated_data.get('due_date', instance.due_date),
            )
        return services.update_transaction_full(
            instance,
            account=validated_data.get('account', instance.account),
            category=validated_data.get('category', instance.category),
            direction=validated_data.get('direction', instance.direction),
            amount=validated_data.get('amount', instance.amount),
            description=validated_data.get('description', instance.description),
            due_date=validated_data.get('due_date', instance.due_date),
        )


class TransactionCreateSerializer(TransactionSerializer):
    """Create only: executed/executed_date become writable, for logging a
    transaction that already happened — matches
    ledger.forms.TransactionCreateForm. services.create_transaction itself
    enforces "executed requires an account". attachment is writable here too
    (attach a receipt right when logging the purchase) but — like
    executed/executed_date — not on the base TransactionSerializer: changing
    it afterward goes through the dedicated {id}/attachment/ action instead,
    matching ledger.views' "changes only via a dedicated view" design."""

    attachment = serializers.FileField(write_only=True, required=False, allow_null=True,
                                        validators=ATTACHMENT_VALIDATORS)
    # Not required here — services.create_transaction fills it in from executed_date when the
    # transaction is created already-executed (see its docstring); required=True is still
    # enforced there (raises, translated to a 400) for a not-yet-executed transaction with no
    # due_date at all.
    due_date = serializers.DateField(required=False)

    class Meta(TransactionSerializer.Meta):
        fields = TransactionSerializer.Meta.fields + ['attachment']
        read_only_fields = ['recurring_source', 'statement', 'created_at', 'updated_at']
        # ModelSerializer auto-generates a UniqueTogetherValidator from Transaction.Meta's
        # unique_occurrence_per_template_per_date constraint (recurring_source + due_date).
        # Its own enforce_required_fields() forces both fields "required" on create — bypassing
        # due_date's required=False above — regardless of the individual field's own setting.
        # Harmless to drop here: recurring_source is read-only on this serializer (never
        # settable via this endpoint — only services.ensure_recurring_horizon ever sets it), so
        # there's no way to create() through here that could actually violate the constraint;
        # the database CheckConstraint is still the real backstop either way.
        validators = []

    def create(self, validated_data):
        return services.create_transaction(
            account=validated_data.get('account'),
            category=validated_data.get('category'),
            direction=validated_data['direction'],
            amount=validated_data['amount'],
            description=validated_data.get('description', ''),
            due_date=validated_data.get('due_date'),
            executed=validated_data.get('executed', False),
            executed_date=validated_data.get('executed_date'),
            attachment=validated_data.get('attachment'),
        )


class ExecuteSerializer(serializers.Serializer):
    executed_date = serializers.DateField(required=False)


class AssignAccountSerializer(serializers.Serializer):
    account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.filter(is_active=True))


class AttachmentUploadSerializer(serializers.Serializer):
    """Body for POST {id}/attachment/ on both TransactionViewSet and TransferViewSet — required
    here (unlike the create-time field above) since posting to this action IS the request to
    set/replace a file; clearing is its own DELETE on the same route instead of a null value, to
    sidestep multipart/form-data having no clean way to express "null" the way JSON does."""
    attachment = serializers.FileField(validators=ATTACHMENT_VALIDATORS)


class RecurringTransactionSerializer(ActiveAccountFieldMixin, serializers.ModelSerializer):
    class Meta:
        model = RecurringTransaction
        fields = [
            'id', 'account', 'category', 'direction', 'amount', 'description',
            'frequency', 'interval', 'start_date', 'end_date', 'is_active',
            'generated_until', 'created_at', 'updated_at',
        ]
        read_only_fields = ['generated_until', 'created_at', 'updated_at']

    def validate(self, attrs):
        # RecurringTransaction.Meta.constraints enforces this at the DB level,
        # but only a Django ModelForm's full_clean() surfaces it as a clean
        # validation error automatically — a plain ModelSerializer does not,
        # so it's replicated here (see ledger/models.py:
        # recurring_end_date_after_start_date).
        end_date = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        start_date = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        if end_date and start_date and end_date < start_date:
            raise serializers.ValidationError({'end_date': 'End date must be on or after the start date.'})
        return attrs

    def create(self, validated_data):
        recurring = super().create(validated_data)
        services.ensure_recurring_horizon(recurring)
        return recurring

    def update(self, instance, validated_data):
        recurring = super().update(instance, validated_data)
        services.regenerate_future_occurrences(recurring)
        return recurring


class TransferSerializer(serializers.Serializer):
    """Read + create. Flat shape mirroring ledger.forms.TransferCreateForm —
    a Transfer is two linked Transaction rows (out_leg/in_leg) under the
    hood, but that pairing isn't meaningful to an API consumer."""

    id = serializers.IntegerField(read_only=True)
    from_account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.filter(is_active=True), required=False, allow_null=True,
    )
    to_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.filter(is_active=True))
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    # Not required — services.create_transfer fills it in from executed_date when the transfer
    # is created already-executed (see create_transaction's docstring for the same rationale).
    due_date = serializers.DateField(required=False)
    executed = serializers.BooleanField(required=False, default=False)
    executed_date = serializers.DateField(required=False, allow_null=True)
    attachment = serializers.FileField(write_only=True, required=False, allow_null=True,
                                        validators=ATTACHMENT_VALIDATORS)
    out_leg_id = serializers.IntegerField(read_only=True)
    in_leg_id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)

    def to_representation(self, instance):
        return {
            'id': instance.pk,
            'from_account': instance.out_leg.account_id,
            'to_account': instance.in_leg.account_id,
            'amount': instance.out_leg.amount,
            'description': instance.out_leg.description,
            'due_date': instance.out_leg.due_date,
            'executed': instance.out_leg.executed,
            'executed_date': instance.out_leg.executed_date,
            'attachment_url': _attachment_url(self.context, instance.attachment, 'api:transfer-attachment', instance.pk),
            'attachment_name': _attachment_name(instance.attachment),
            'out_leg_id': instance.out_leg_id,
            'in_leg_id': instance.in_leg_id,
            'created_at': instance.created_at,
        }

    def validate(self, attrs):
        # services.create_transfer enforces neither of these rules itself —
        # both currently live only in ledger.forms.TransferCreateForm.clean(),
        # replicated here verbatim.
        from_account = attrs.get('from_account')
        to_account = attrs.get('to_account')
        if attrs.get('executed') and not from_account:
            raise serializers.ValidationError(
                {'from_account': 'Assign a source account before marking this as already executed.'})
        if from_account and to_account and from_account.pk == to_account.pk:
            raise serializers.ValidationError({'to_account': 'Source and destination must be different accounts.'})
        return attrs

    def create(self, validated_data):
        return services.create_transfer(
            from_account=validated_data.get('from_account'),
            to_account=validated_data['to_account'],
            amount=validated_data['amount'],
            description=validated_data.get('description', ''),
            due_date=validated_data.get('due_date'),
            executed=validated_data.get('executed', False),
            executed_date=validated_data.get('executed_date'),
            attachment=validated_data.get('attachment'),
        )


class TransferUpdateSerializer(serializers.Serializer):
    """Update only — amount/description/due_date, matching
    ledger.forms.TransferEditForm exactly (accounts aren't reassignable;
    services.update_transfer doesn't support that either — "delete and
    recreate" covers that rare case, per its own docstring)."""

    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    due_date = serializers.DateField()

    def to_representation(self, instance):
        return TransferSerializer(instance, context=self.context).data

    def update(self, instance, validated_data):
        return services.update_transfer(
            instance,
            amount=validated_data['amount'],
            description=validated_data.get('description', ''),
            due_date=validated_data['due_date'],
        )


class CreditCardStatementSerializer(serializers.ModelSerializer):
    is_paid = serializers.BooleanField(read_only=True)

    class Meta:
        model = CreditCardStatement
        fields = [
            'id', 'account', 'cut_date', 'due_date', 'statement_balance',
            'payment_obligation', 'is_paid', 'created_at',
        ]
        read_only_fields = fields


# ---------- Reporting (summary / projection) ----------
# Read-only wrappers around the plain dicts services.account_summary() /
# services.project_balances() return — see those functions' docstrings.

class AccountSummarySerializer(serializers.Serializer):
    total_assets = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_card_debt = serializers.DecimalField(max_digits=10, decimal_places=2)
    net_worth = serializers.DecimalField(max_digits=10, decimal_places=2)
    accounts = AccountSerializer(many=True)
    overdue = TransactionSerializer(many=True)
    upcoming = TransactionSerializer(many=True)
    unassigned = TransactionSerializer(many=True)


class ProjectionQuerySerializer(serializers.Serializer):
    """Validates the ?target_date=&account= query params — mirrors
    ledger.forms.ProjectionForm."""
    target_date = serializers.DateField()
    account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.filter(is_active=True), required=False, allow_null=True, default=None,
    )


class ProjectionRowSerializer(serializers.Serializer):
    transaction = TransactionSerializer()
    running_balance = serializers.DecimalField(max_digits=10, decimal_places=2)


class UnassignedProjectionRowSerializer(serializers.Serializer):
    transaction = TransactionSerializer()
    running_total = serializers.DecimalField(max_digits=10, decimal_places=2)


class ProjectionAccountSummarySerializer(serializers.Serializer):
    account = AccountSerializer()
    current_balance = serializers.DecimalField(max_digits=10, decimal_places=2)
    projected_balance = serializers.DecimalField(max_digits=10, decimal_places=2)


class ProjectionAccountDetailSerializer(ProjectionAccountSummarySerializer):
    rows = ProjectionRowSerializer(many=True)


class ProjectionSummarySerializer(serializers.Serializer):
    target_date = serializers.DateField()
    combined_current = serializers.DecimalField(max_digits=10, decimal_places=2)
    combined_projected = serializers.DecimalField(max_digits=10, decimal_places=2)
    unassigned_total = serializers.DecimalField(max_digits=10, decimal_places=2)
    results = ProjectionAccountSummarySerializer(many=True)


class ProjectionDetailSerializer(ProjectionSummarySerializer):
    results = ProjectionAccountDetailSerializer(many=True)
    unassigned_rows = UnassignedProjectionRowSerializer(many=True)
