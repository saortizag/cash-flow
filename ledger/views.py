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
    CategoryForm,
    ExecuteTransactionForm,
    ProjectionForm,
    RecurringTransactionForm,
    TransactionCreateForm,
    TransactionEditForm,
    TransactionOpenFieldsForm,
)
from .models import Account, Category, RecurringTransaction, Transaction


@login_required
def dashboard(request):
    services.ensure_recurring_horizon_for_all_active()
    today = timezone.localdate()
    accounts = Account.objects.filter(is_active=True)
    total_balance = sum((a.current_balance for a in accounts), start=Decimal('0.00'))
    overdue = Transaction.objects.filter(executed=False, due_date__lt=today).select_related('account', 'category')
    upcoming = Transaction.objects.filter(executed=False, due_date__gte=today).select_related('account', 'category')[:10]
    return render(request, 'ledger/dashboard.html', {
        'accounts': accounts,
        'total_balance': total_balance,
        'overdue': overdue,
        'upcoming': upcoming,
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
    qs = Transaction.objects.select_related('account', 'category').order_by('-due_date')

    account_id = request.GET.get('account')
    category_id = request.GET.get('category')
    executed = request.GET.get('executed')
    direction = request.GET.get('direction')
    if account_id and account_id.isdigit():
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
    if txn.executed:
        messages.info(request, 'Transaction is already executed.')
        return redirect('ledger:transaction_list')
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
    try:
        services.unexecute_transaction(txn)
        messages.success(request, f'Un-executed — {txn.account.name} balance reversed.')
    except ValidationError as e:
        messages.error(request, str(e))
    return redirect('ledger:transaction_list')


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

        context.update({
            'results': results,
            'target_date': target_date,
            'combined_current': sum((r['current_balance'] for r in results), Decimal('0.00')),
            'combined_projected': sum((r['projected_balance'] for r in results), Decimal('0.00')),
        })

    return render(request, 'ledger/projection.html', context)
