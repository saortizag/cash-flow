from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from . import services
from .forms import (
    AccountForm,
    AssignAccountForm,
    CategoryForm,
    CreditCardBootstrapForm,
    ExecuteTransactionForm,
    ProjectionForm,
    RecurringTransactionForm,
    TransactionCreateForm,
    TransactionEditForm,
    TransactionOpenFieldsForm,
    TransferCreateForm,
    TransferEditForm,
    TransferExecuteForm,
)
from .models import Account, Category, RecurringTransaction, Transaction, Transfer


def _transfer_for_leg(txn):
    """None if `txn` is a plain Transaction; the Transfer it belongs to if it's
    one of a pair's legs. Reverse OneToOneField accessors raise DoesNotExist
    (which Django deliberately makes an AttributeError subclass) rather than
    returning None, so getattr(..., None) is the correct existence check."""
    return getattr(txn, 'transfer_as_source', None) or getattr(txn, 'transfer_as_destination', None)


@login_required
def dashboard(request):
    services.ensure_recurring_horizon_for_all_active()
    services.close_statements_if_due_for_all_cards()
    today = timezone.localdate()
    accounts = Account.objects.filter(is_active=True)
    asset_accounts = [a for a in accounts if a.account_type != Account.AccountType.CREDIT_CARD]
    card_accounts = [a for a in accounts if a.account_type == Account.AccountType.CREDIT_CARD]
    total_assets = sum((a.current_balance for a in asset_accounts), start=Decimal('0.00'))
    total_card_debt = -sum((a.current_balance for a in card_accounts), start=Decimal('0.00'))
    net_worth = total_assets - total_card_debt

    overdue = Transaction.objects.filter(
        executed=False, due_date__lt=today, account__isnull=False,
    ).select_related('account', 'category')
    upcoming = Transaction.objects.filter(
        executed=False, due_date__gte=today, account__isnull=False,
    ).select_related('account', 'category')[:10]
    unassigned = Transaction.objects.filter(
        executed=False, account__isnull=True,
    ).select_related('category').order_by('due_date')

    return render(request, 'ledger/dashboard.html', {
        'accounts': accounts,
        'total_assets': total_assets,
        'total_card_debt': total_card_debt,
        'net_worth': net_worth,
        'overdue': overdue,
        'upcoming': upcoming,
        'unassigned': unassigned,
    })


# ---- Account CRUD ----

class AccountListView(LoginRequiredMixin, ListView):
    model = Account
    template_name = 'ledger/account_list.html'
    context_object_name = 'accounts'


class AccountCreateView(LoginRequiredMixin, CreateView):
    model = Account
    form_class = AccountForm
    template_name = 'ledger/account_form.html'
    success_url = reverse_lazy('ledger:account_list')


class AccountUpdateView(LoginRequiredMixin, UpdateView):
    model = Account
    form_class = AccountForm
    template_name = 'ledger/account_form.html'
    success_url = reverse_lazy('ledger:account_list')


class AccountDeleteView(LoginRequiredMixin, DeleteView):
    model = Account
    template_name = 'ledger/account_confirm_delete.html'
    success_url = reverse_lazy('ledger:account_list')

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, 'Cannot delete: this account has transaction history. Deactivate it instead.')
            return redirect('ledger:account_list')


@login_required
def credit_card_bootstrap(request):
    if request.method == 'POST':
        form = CreditCardBootstrapForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            services.bootstrap_statement(cd['account'], cd['amount_owed'], cd['due_date'])
            messages.success(request, f"Recorded {cd['amount_owed']} owed on {cd['account'].name}, due {cd['due_date']}.")
            return redirect('ledger:account_list')
    else:
        form = CreditCardBootstrapForm()
    return render(request, 'ledger/credit_card_bootstrap.html', {'form': form})


# ---- Category CRUD ----

class CategoryListView(LoginRequiredMixin, ListView):
    model = Category
    template_name = 'ledger/category_list.html'
    context_object_name = 'categories'


class CategoryCreateView(LoginRequiredMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = 'ledger/category_form.html'
    success_url = reverse_lazy('ledger:category_list')


class CategoryUpdateView(LoginRequiredMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = 'ledger/category_form.html'
    success_url = reverse_lazy('ledger:category_list')


class CategoryDeleteView(LoginRequiredMixin, DeleteView):
    model = Category
    template_name = 'ledger/category_confirm_delete.html'
    success_url = reverse_lazy('ledger:category_list')


# ---- Transaction ----

@login_required
def transaction_list(request):
    # transfer_as_source/transfer_as_destination are select_related (not
    # prefetch_related) even though they're reverse relations — Django
    # supports this for O2O specifically, since it's still a single-row join.
    # Without it, the template's per-row transfer badge check triggers up to
    # two extra queries per row.
    qs = Transaction.objects.select_related(
        'account', 'category', 'transfer_as_source', 'transfer_as_destination',
    ).order_by('-due_date')

    account_id = request.GET.get('account')
    category_id = request.GET.get('category')
    executed = request.GET.get('executed')
    direction = request.GET.get('direction')
    if account_id == 'unassigned':
        qs = qs.filter(account__isnull=True)
    elif account_id and account_id.isdigit():
        qs = qs.filter(account_id=account_id)
    if category_id and category_id.isdigit():
        qs = qs.filter(category_id=category_id)
    if executed in ('true', 'false'):
        qs = qs.filter(executed=(executed == 'true'))
    if direction in (Transaction.Direction.IN, Transaction.Direction.OUT):
        qs = qs.filter(direction=direction)

    return render(request, 'ledger/transaction_list.html', {
        'transactions': qs,
        'accounts': Account.objects.filter(is_active=True),
        'categories': Category.objects.all(),
    })


@login_required
def transaction_create(request):
    if request.method == 'POST':
        form = TransactionCreateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            services.create_transaction(
                account=cd['account'], category=cd['category'], direction=cd['direction'],
                amount=cd['amount'], description=cd['description'], due_date=cd['due_date'],
                executed=cd['executed'], executed_date=cd['executed_date'],
            )
            messages.success(request, 'Transaction created.')
            return redirect('ledger:transaction_list')
    else:
        form = TransactionCreateForm(initial={'due_date': timezone.localdate()})
    return render(request, 'ledger/transaction_form.html', {'form': form})


@login_required
def transaction_update(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    transfer = _transfer_for_leg(txn)
    if transfer:
        messages.info(request, 'This is part of a transfer — manage it from the Transfers page.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    form_class = TransactionOpenFieldsForm if txn.executed else TransactionEditForm
    if request.method == 'POST':
        form = form_class(request.POST, instance=txn)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                if txn.executed:
                    services.update_transaction_open_fields(
                        txn, category=cd['category'], description=cd['description'], due_date=cd['due_date'],
                    )
                else:
                    services.update_transaction_full(
                        txn, account=cd['account'], category=cd['category'], direction=cd['direction'],
                        amount=cd['amount'], description=cd['description'], due_date=cd['due_date'],
                    )
                messages.success(request, 'Transaction updated.')
                return redirect('ledger:transaction_list')
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = form_class(instance=txn)
    return render(request, 'ledger/transaction_form.html', {'form': form, 'transaction': txn})


@login_required
def transaction_delete(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    transfer = _transfer_for_leg(txn)
    if transfer:
        messages.info(request, 'This is part of a transfer — manage it from the Transfers page.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    if txn.executed:
        messages.error(request, 'Cannot delete an executed transaction. Un-execute it first.')
        return redirect('ledger:transaction_list')
    if request.method == 'POST':
        services.delete_transaction(txn)
        messages.success(request, 'Transaction deleted.')
        return redirect('ledger:transaction_list')
    return render(request, 'ledger/transaction_confirm_delete.html', {'transaction': txn})


@login_required
def transaction_execute(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    transfer = _transfer_for_leg(txn)
    if transfer:
        messages.info(request, 'This is part of a transfer — manage it from the Transfers page.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    if txn.executed:
        messages.info(request, 'Transaction is already executed.')
        return redirect('ledger:transaction_list')
    if txn.account_id is None:
        messages.error(request, 'Assign an account before executing this transaction.')
        return redirect('ledger:transaction_assign_account', pk=txn.pk)
    if request.method == 'POST':
        form = ExecuteTransactionForm(request.POST)
        if form.is_valid():
            services.execute_transaction(txn, executed_date=form.cleaned_data['executed_date'])
            messages.success(request, f'Executed — {txn.account.name} balance updated.')
            return redirect('ledger:transaction_list')
    else:
        form = ExecuteTransactionForm()
    return render(request, 'ledger/transaction_confirm_execute.html', {'form': form, 'transaction': txn})


@login_required
@require_POST
def transaction_unexecute(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    transfer = _transfer_for_leg(txn)
    if transfer:
        messages.info(request, 'This is part of a transfer — manage it from the Transfers page.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    try:
        services.unexecute_transaction(txn)
        messages.success(request, f'Un-executed — {txn.account.name} balance reversed.')
    except ValidationError as e:
        messages.error(request, str(e))
    return redirect('ledger:transaction_list')


@login_required
def transaction_assign_account(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    transfer = _transfer_for_leg(txn)
    success_url = redirect('ledger:transfer_detail', pk=transfer.pk) if transfer else redirect('ledger:transaction_list')
    if txn.account_id is not None:
        messages.info(request, 'This transaction already has an account assigned.')
        return success_url
    if request.method == 'POST':
        form = AssignAccountForm(request.POST)
        if form.is_valid():
            services.assign_account(txn, form.cleaned_data['account'])
            messages.success(request, f'Assigned to {txn.account.name}.')
            return success_url
    else:
        form = AssignAccountForm()
    return render(request, 'ledger/transaction_assign_account.html', {
        'form': form, 'transaction': txn, 'transfer': transfer,
    })


# ---- Transfers ----

@login_required
def transfer_list(request):
    transfers = Transfer.objects.select_related(
        'out_leg', 'out_leg__account', 'in_leg', 'in_leg__account',
    ).order_by('-out_leg__due_date')
    return render(request, 'ledger/transfer_list.html', {'transfers': transfers})


@login_required
def transfer_create(request):
    if request.method == 'POST':
        form = TransferCreateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            services.create_transfer(
                from_account=cd['from_account'], to_account=cd['to_account'], amount=cd['amount'],
                description=cd['description'], due_date=cd['due_date'],
                executed=cd['executed'], executed_date=cd['executed_date'],
            )
            messages.success(request, 'Transfer created.')
            return redirect('ledger:transfer_list')
    else:
        form = TransferCreateForm(initial={'due_date': timezone.localdate()})
    return render(request, 'ledger/transfer_form.html', {'form': form})


@login_required
def transfer_detail(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    return render(request, 'ledger/transfer_detail.html', {'transfer': transfer})


@login_required
def transfer_update(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    if transfer.out_leg.executed or transfer.in_leg.executed:
        messages.error(request, 'Cannot edit an executed transfer. Un-execute it first.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    if request.method == 'POST':
        form = TransferEditForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                services.update_transfer(transfer, amount=cd['amount'], description=cd['description'],
                                          due_date=cd['due_date'])
                messages.success(request, 'Transfer updated.')
                return redirect('ledger:transfer_detail', pk=transfer.pk)
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = TransferEditForm(initial={
            'amount': transfer.out_leg.amount, 'description': transfer.out_leg.description,
            'due_date': transfer.out_leg.due_date,
        })
    return render(request, 'ledger/transfer_form.html', {'form': form, 'transfer': transfer})


@login_required
def transfer_delete(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    if transfer.out_leg.executed or transfer.in_leg.executed:
        messages.error(request, 'Cannot delete an executed transfer. Un-execute it first.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    if request.method == 'POST':
        services.delete_transfer(transfer)
        messages.success(request, 'Transfer deleted.')
        return redirect('ledger:transfer_list')
    return render(request, 'ledger/transfer_confirm_delete.html', {'transfer': transfer})


@login_required
def transfer_execute(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    if transfer.out_leg.executed:
        messages.info(request, 'Transfer is already executed.')
        return redirect('ledger:transfer_detail', pk=transfer.pk)
    if transfer.out_leg.account_id is None:
        messages.error(request, 'Assign a source account before executing this transfer.')
        return redirect('ledger:transaction_assign_account', pk=transfer.out_leg.pk)
    if request.method == 'POST':
        form = TransferExecuteForm(request.POST)
        if form.is_valid():
            services.execute_transfer(transfer, executed_date=form.cleaned_data['executed_date'])
            messages.success(request, 'Transfer executed.')
            return redirect('ledger:transfer_detail', pk=transfer.pk)
    else:
        form = TransferExecuteForm()
    return render(request, 'ledger/transfer_confirm_execute.html', {'form': form, 'transfer': transfer})


@login_required
@require_POST
def transfer_unexecute(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        services.unexecute_transfer(transfer)
        messages.success(request, 'Transfer un-executed.')
    except ValidationError as e:
        messages.error(request, str(e))
    return redirect('ledger:transfer_detail', pk=transfer.pk)


# ---- Recurring ----

class RecurringListView(LoginRequiredMixin, ListView):
    model = RecurringTransaction
    template_name = 'ledger/recurringtransaction_list.html'
    context_object_name = 'recurring_transactions'


class RecurringCreateView(LoginRequiredMixin, CreateView):
    model = RecurringTransaction
    form_class = RecurringTransactionForm
    template_name = 'ledger/recurringtransaction_form.html'
    success_url = reverse_lazy('ledger:recurring_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        services.ensure_recurring_horizon(self.object)
        messages.success(self.request, 'Recurring template created; occurrences generated.')
        return response


@login_required
def recurring_update(request, pk):
    recurring = get_object_or_404(RecurringTransaction, pk=pk)
    if request.method == 'POST':
        form = RecurringTransactionForm(request.POST, instance=recurring)
        if form.is_valid():
            form.save()
            services.regenerate_future_occurrences(recurring)
            messages.success(request, 'Template updated; future pending occurrences regenerated.')
            return redirect('ledger:recurring_list')
    else:
        form = RecurringTransactionForm(instance=recurring)
    return render(request, 'ledger/recurringtransaction_form.html', {'form': form, 'recurring': recurring})


@login_required
def recurring_delete(request, pk):
    recurring = get_object_or_404(RecurringTransaction, pk=pk)
    if request.method == 'POST':
        services.delete_recurring(recurring)
        messages.success(request, 'Template deleted; future pending occurrences removed, history kept.')
        return redirect('ledger:recurring_list')
    return render(request, 'ledger/recurringtransaction_confirm_delete.html', {'recurring': recurring})


@login_required
@require_POST
def recurring_deactivate(request, pk):
    recurring = get_object_or_404(RecurringTransaction, pk=pk)
    services.deactivate_recurring(recurring)
    messages.success(request, 'Template deactivated; future pending occurrences removed.')
    return redirect('ledger:recurring_list')


# ---- Projection ----

@login_required
def projection_view(request):
    services.ensure_recurring_horizon_for_all_active()
    services.close_statements_if_due_for_all_cards()
    initial = {'target_date': timezone.localdate() + relativedelta(months=1)}
    form = ProjectionForm(request.GET or initial)
    context = {'form': form}

    if form.is_valid():
        target_date = form.cleaned_data['target_date']
        picked_account = form.cleaned_data.get('account')
        accounts = [picked_account] if picked_account else list(Account.objects.filter(is_active=True))

        results = []
        for acct in accounts:
            pending = Transaction.objects.filter(
                account=acct, executed=False, due_date__lte=target_date,
            ).select_related('category').order_by('due_date', 'id')
            running = acct.current_balance
            rows = []
            for txn in pending:
                running += services.signed_amount(txn.direction, txn.amount)
                rows.append({'transaction': txn, 'running_balance': running})
            results.append({
                'account': acct,
                'current_balance': acct.current_balance,
                'projected_balance': running,
                'rows': rows,
            })

        combined_current = sum((r['current_balance'] for r in results), Decimal('0.00'))
        combined_projected = sum((r['projected_balance'] for r in results), Decimal('0.00'))

        # Unassigned obligations belong to no specific account, so they only
        # ever factor into the "all accounts combined" view, never a
        # single-account one (that would misattribute them to one account).
        unassigned_rows = []
        unassigned_total = Decimal('0.00')
        if picked_account is None:
            unassigned_pending = Transaction.objects.filter(
                account__isnull=True, executed=False, due_date__lte=target_date,
            ).select_related('category').order_by('due_date', 'id')
            for txn in unassigned_pending:
                unassigned_total += services.signed_amount(txn.direction, txn.amount)
                unassigned_rows.append({'transaction': txn, 'running_total': unassigned_total})
            combined_projected += unassigned_total

        context.update({
            'results': results,
            'target_date': target_date,
            'combined_current': combined_current,
            'combined_projected': combined_projected,
            'unassigned_rows': unassigned_rows,
            'unassigned_total': unassigned_total,
        })

    return render(request, 'ledger/projection.html', context)
