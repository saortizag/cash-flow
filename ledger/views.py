import os

from dateutil.relativedelta import relativedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from . import services
from .forms import (
    AccountForm,
    AssignAccountForm,
    AttachmentForm,
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


def _attachment_display_name(field_file):
    """The original filename, stripped of the upload_to storage path (a random per-upload
    directory — see models.attachment_upload_path) — what a user actually recognizes."""
    return os.path.basename(field_file.name) if field_file else None


def _serve_attachment(field_file):
    if not field_file:
        raise Http404('No attachment.')
    return FileResponse(field_file.open('rb'), as_attachment=True, filename=_attachment_display_name(field_file))


@login_required
def dashboard(request):
    services.ensure_recurring_horizon_for_all_active()
    services.close_statements_if_due_for_all_cards()
    return render(request, 'ledger/dashboard.html', services.account_summary())


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
        form = TransactionCreateForm(request.POST, request.FILES)
        if form.is_valid():
            cd = form.cleaned_data
            services.create_transaction(
                account=cd['account'], category=cd['category'], direction=cd['direction'],
                amount=cd['amount'], description=cd['description'], due_date=cd['due_date'],
                executed=cd['executed'], executed_date=cd['executed_date'], attachment=cd['attachment'],
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


@login_required
def transaction_attachment(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    transfer = _transfer_for_leg(txn)
    if transfer:
        return redirect('ledger:transfer_attachment', pk=transfer.pk)
    if request.method == 'POST':
        # initial= matters here, not just on the GET render below: FileField.clean() falls
        # back to it when nothing was submitted, which is how "no change" is distinguished
        # from "clear" (the ClearableFileInput checkbox, independent of initial) — see
        # AttachmentForm's docstring.
        form = AttachmentForm(request.POST, request.FILES, initial={'attachment': txn.attachment})
        if form.is_valid():
            data = form.cleaned_data['attachment']
            if data is not None:  # None = nothing submitted, i.e. no change
                services.update_transaction_attachment(txn, data or None)  # False (clear) -> None
                messages.success(request, 'Attachment updated.' if data else 'Attachment removed.')
            return redirect('ledger:transaction_list')
    else:
        form = AttachmentForm(initial={'attachment': txn.attachment})
    return render(request, 'ledger/transaction_attachment.html', {
        'form': form, 'transaction': txn, 'attachment_name': _attachment_display_name(txn.attachment),
    })


@login_required
def transaction_attachment_download(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    return _serve_attachment(txn.attachment)


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
        form = TransferCreateForm(request.POST, request.FILES)
        if form.is_valid():
            cd = form.cleaned_data
            services.create_transfer(
                from_account=cd['from_account'], to_account=cd['to_account'], amount=cd['amount'],
                description=cd['description'], due_date=cd['due_date'],
                executed=cd['executed'], executed_date=cd['executed_date'], attachment=cd['attachment'],
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
def transfer_attachment(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    if request.method == 'POST':
        form = AttachmentForm(request.POST, request.FILES, initial={'attachment': transfer.attachment})
        if form.is_valid():
            data = form.cleaned_data['attachment']
            if data is not None:  # None = nothing submitted, i.e. no change
                services.update_transfer_attachment(transfer, data or None)  # False (clear) -> None
                messages.success(request, 'Attachment updated.' if data else 'Attachment removed.')
            return redirect('ledger:transfer_detail', pk=transfer.pk)
    else:
        form = AttachmentForm(initial={'attachment': transfer.attachment})
    return render(request, 'ledger/transfer_attachment.html', {
        'form': form, 'transfer': transfer, 'attachment_name': _attachment_display_name(transfer.attachment),
    })


@login_required
def transfer_attachment_download(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    return _serve_attachment(transfer.attachment)


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
        context.update(services.project_balances(target_date, account=picked_account))

    return render(request, 'ledger/projection.html', context)
