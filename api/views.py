import os

from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ledger import services
from ledger.models import Account, Category, CreditCardStatement, RecurringTransaction, Transaction, Transfer

from .filters import TransactionFilter
from .serializers import (
    AccountSerializer,
    AccountSummarySerializer,
    AssignAccountSerializer,
    AttachmentUploadSerializer,
    CategorySerializer,
    CreditCardBootstrapSerializer,
    CreditCardStatementSerializer,
    ExecuteSerializer,
    ProjectionDetailSerializer,
    ProjectionQuerySerializer,
    ProjectionSummarySerializer,
    RecurringTransactionSerializer,
    TransactionCreateSerializer,
    TransactionSerializer,
    TransferSerializer,
    TransferUpdateSerializer,
)


def _transfer_for_leg(txn):
    """None if `txn` is a plain Transaction; the Transfer it belongs to if
    it's one of a pair's legs. Same check as ledger.views._transfer_for_leg,
    kept as an independent copy since that one is private to the template
    views' module and this app deliberately doesn't import across view
    layers (only ledger.services is shared)."""
    return getattr(txn, 'transfer_as_source', None) or getattr(txn, 'transfer_as_destination', None)


def _serve_attachment(field_file):
    if not field_file:
        raise Http404('No attachment.')
    return FileResponse(field_file.open('rb'), as_attachment=True, filename=os.path.basename(field_file.name))


class AccountViewSet(viewsets.ModelViewSet):
    queryset = Account.objects.all()
    serializer_class = AccountSerializer

    @action(detail=True, methods=['post'], url_path='bootstrap-statement')
    def bootstrap_statement(self, request, pk=None):
        account = self.get_object()
        if account.account_type != Account.AccountType.CREDIT_CARD:
            raise DRFValidationError({'detail': 'This action is only valid for credit card accounts.'})
        serializer = CreditCardBootstrapSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        statement = services.bootstrap_statement(
            account, serializer.validated_data['amount_owed'], serializer.validated_data['due_date'],
        )
        return Response(CreditCardStatementSerializer(statement).data, status=status.HTTP_201_CREATED)


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class TransactionViewSet(viewsets.ModelViewSet):
    # Ascending due_date (earliest first), 'id' tiebreak — matches the web transaction list's
    # own default and stable-ordering rationale (see ledger.views.transaction_list). Paginated
    # at PAGE_SIZE (50, set globally in REST_FRAMEWORK) like every other list endpoint here.
    queryset = Transaction.objects.select_related(
        'account', 'category', 'transfer_as_source', 'transfer_as_destination',
    ).order_by('due_date', 'id')
    filterset_class = TransactionFilter

    def get_serializer_class(self):
        if self.action == 'create':
            return TransactionCreateSerializer
        return TransactionSerializer

    def _reject_transfer_leg(self, txn):
        transfer = _transfer_for_leg(txn)
        if transfer:
            raise DRFValidationError({
                'detail': 'This transaction is part of a transfer — manage it via /api/v1/transfers/.',
                'transfer_id': transfer.pk,
            })

    def update(self, request, *args, **kwargs):
        self._reject_transfer_leg(self.get_object())
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self._reject_transfer_leg(self.get_object())
        return super().partial_update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        self._reject_transfer_leg(instance)
        services.delete_transaction(instance)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        txn = self.get_object()
        self._reject_transfer_leg(txn)
        serializer = ExecuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn = services.execute_transaction(txn, executed_date=serializer.validated_data.get('executed_date'))
        return Response(TransactionSerializer(txn, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'])
    def unexecute(self, request, pk=None):
        txn = self.get_object()
        self._reject_transfer_leg(txn)
        txn = services.unexecute_transaction(txn)
        return Response(TransactionSerializer(txn, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'], url_path='assign-account')
    def assign_account(self, request, pk=None):
        txn = self.get_object()
        # Deliberately NOT guarded by _reject_transfer_leg — assigning an
        # account to an unassigned transfer leg is how a credit-card
        # statement's auto-generated payment obligation gets funded, matching
        # ledger.views.transaction_assign_account exactly.
        serializer = AssignAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn = services.assign_account(txn, serializer.validated_data['account'])
        return Response(TransactionSerializer(txn, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['get', 'post', 'delete'], url_path='attachment')
    def attachment(self, request, pk=None):
        """GET streams the file (404 if none — allowed even on a transfer leg, no reason to
        block a read); POST sets/replaces it; DELETE clears it. Mutating a transfer leg's
        attachment directly is blocked, same as every other mutation on a leg — manage it via
        the transfer's own {id}/attachment/ action instead."""
        txn = self.get_object()
        if request.method == 'GET':
            return _serve_attachment(txn.attachment)
        self._reject_transfer_leg(txn)
        if request.method == 'DELETE':
            services.update_transaction_attachment(txn, None)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = AttachmentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_transaction_attachment(txn, serializer.validated_data['attachment'])
        return Response(TransactionSerializer(txn, context=self.get_serializer_context()).data)


class RecurringTransactionViewSet(viewsets.ModelViewSet):
    queryset = RecurringTransaction.objects.all()
    serializer_class = RecurringTransactionSerializer

    def perform_destroy(self, instance):
        services.delete_recurring(instance)

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        recurring = self.get_object()
        services.deactivate_recurring(recurring)
        recurring.refresh_from_db()
        return Response(RecurringTransactionSerializer(recurring).data)


class TransferViewSet(viewsets.ModelViewSet):
    queryset = Transfer.objects.select_related(
        'out_leg', 'out_leg__account', 'in_leg', 'in_leg__account',
    ).order_by('-out_leg__due_date')

    def get_serializer_class(self):
        if self.action in ('update', 'partial_update'):
            return TransferUpdateSerializer
        return TransferSerializer

    def perform_destroy(self, instance):
        services.delete_transfer(instance)

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        transfer = self.get_object()
        serializer = ExecuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        transfer = services.execute_transfer(transfer, executed_date=serializer.validated_data.get('executed_date'))
        return Response(TransferSerializer(transfer, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'])
    def unexecute(self, request, pk=None):
        transfer = self.get_object()
        transfer = services.unexecute_transfer(transfer)
        return Response(TransferSerializer(transfer, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['get', 'post', 'delete'], url_path='attachment')
    def attachment(self, request, pk=None):
        """Same GET/POST/DELETE shape as TransactionViewSet.attachment — see its docstring.
        Always allowed regardless of executed state (services.update_transfer_attachment,
        deliberately not services.update_transfer, which locks once executed)."""
        transfer = self.get_object()
        if request.method == 'GET':
            return _serve_attachment(transfer.attachment)
        if request.method == 'DELETE':
            services.update_transfer_attachment(transfer, None)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = AttachmentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_transfer_attachment(transfer, serializer.validated_data['attachment'])
        return Response(TransferSerializer(transfer, context=self.get_serializer_context()).data)


class CreditCardStatementViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CreditCardStatement.objects.select_related('account', 'payment_obligation').all()
    serializer_class = CreditCardStatementSerializer
    filterset_fields = ['account']


class SummaryView(APIView):
    """Assets / card debt / net worth + overdue / upcoming / unassigned —
    the API equivalent of the template dashboard, backed by the same
    services.account_summary()."""

    def get(self, request):
        services.ensure_recurring_horizon_for_all_active()
        services.close_statements_if_due_for_all_cards()
        context = {'request': request}
        return Response(AccountSummarySerializer(services.account_summary(), context=context).data)


class BaseProjectionView(APIView):
    serializer_class = None

    def get(self, request):
        services.ensure_recurring_horizon_for_all_active()
        services.close_statements_if_due_for_all_cards()
        query = ProjectionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = services.project_balances(
            query.validated_data['target_date'], account=query.validated_data.get('account'),
        )
        return Response(self.serializer_class(data, context={'request': request}).data)


class ProjectionSummaryView(BaseProjectionView):
    serializer_class = ProjectionSummarySerializer


class ProjectionDetailView(BaseProjectionView):
    serializer_class = ProjectionDetailSerializer
