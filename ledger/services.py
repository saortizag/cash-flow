"""
The only sanctioned way to apply a Transaction's execution effect to
Account.current_balance, or to materialize RecurringTransaction occurrences.
Views and admin must call through here rather than toggling
Transaction.executed or writing Transaction rows directly, so the balance and
the recurring-generated rows stay consistent everywhere.

(current_balance can *also* be edited directly, e.g. via the Account form or
admin, when the user wants to set an opening balance or reconcile against a
real bank statement — that's an intentional, separate feature, not something
this module needs to guard against.)

Every function that checks Transaction.executed re-fetches the row under
select_for_update() rather than trusting the caller's possibly-stale in-memory
object — two concurrent calls for the same Transaction (double form submit,
two tabs, a background job racing a request) must not both apply a balance
delta. Lock ordering is always Transaction row first, then Account row, to
avoid deadlocking against the reverse order.
"""

from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import Account, RecurringTransaction, Transaction

DEFAULT_HORIZON_MONTHS = 12

SIGN = {
    Transaction.Direction.IN: Decimal('1'),
    Transaction.Direction.OUT: Decimal('-1'),
}


def signed_amount(direction, amount):
    return SIGN[direction] * amount


# ---------- Transaction lifecycle ----------

@db_transaction.atomic
def execute_transaction(txn, executed_date=None):
    locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if locked_txn.executed:
        raise ValidationError('Transaction is already executed.')
    account = Account.objects.select_for_update().get(pk=locked_txn.account_id)
    account.current_balance += signed_amount(locked_txn.direction, locked_txn.amount)
    account.save(update_fields=['current_balance', 'updated_at'])
    locked_txn.executed = True
    locked_txn.executed_date = executed_date or timezone.localdate()
    locked_txn.save(update_fields=['executed', 'executed_date', 'updated_at'])
    txn.executed = locked_txn.executed
    txn.executed_date = locked_txn.executed_date
    return txn


@db_transaction.atomic
def unexecute_transaction(txn):
    locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if not locked_txn.executed:
        raise ValidationError('Transaction is not executed.')
    account = Account.objects.select_for_update().get(pk=locked_txn.account_id)
    account.current_balance -= signed_amount(locked_txn.direction, locked_txn.amount)
    account.save(update_fields=['current_balance', 'updated_at'])
    locked_txn.executed = False
    locked_txn.executed_date = None
    locked_txn.save(update_fields=['executed', 'executed_date', 'updated_at'])
    txn.executed = locked_txn.executed
    txn.executed_date = locked_txn.executed_date
    return txn


@db_transaction.atomic
def create_transaction(*, account, category, direction, amount, description, due_date,
                        executed=False, executed_date=None, recurring_source=None):
    """Create a Transaction. If executed=True (logging something that already
    happened), apply the balance effect atomically as part of the same call."""
    txn = Transaction.objects.create(
        account=account, category=category, direction=direction, amount=amount,
        description=description, due_date=due_date, recurring_source=recurring_source,
    )
    if executed:
        execute_transaction(txn, executed_date=executed_date)
    return txn


@db_transaction.atomic
def update_transaction_full(txn, *, account, category, direction, amount, description, due_date):
    """Full edit of account/direction/amount/etc. Only valid while executed=False.
    TransactionEditForm is only ever shown for unexecuted transactions; this is
    the backstop for any other caller (admin, shell, future code)."""
    locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if locked_txn.executed:
        raise ValidationError('Cannot edit account/direction/amount on an executed transaction. Un-execute it first.')
    locked_txn.account = account
    locked_txn.category = category
    locked_txn.direction = direction
    locked_txn.amount = amount
    locked_txn.description = description
    locked_txn.due_date = due_date
    locked_txn.save(update_fields=['account', 'category', 'direction', 'amount',
                                    'description', 'due_date', 'updated_at'])
    return locked_txn


def update_transaction_open_fields(txn, *, category, description, due_date):
    """Always allowed regardless of executed state — these fields never affect
    account balance or the projection sum for already-executed rows."""
    txn.category = category
    txn.description = description
    txn.due_date = due_date
    txn.save(update_fields=['category', 'description', 'due_date', 'updated_at'])
    return txn


@db_transaction.atomic
def delete_transaction(txn):
    locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if locked_txn.executed:
        raise ValidationError('Cannot delete an executed transaction. Un-execute it first.')
    locked_txn.delete()


# ---------- Recurring generation ----------

def _occurrence_date(recurring, n):
    """Occurrence #n's due_date, always computed relative to the FIXED start_date
    anchor (never by repeatedly adding relativedelta to the previous date). This
    keeps month-end dates from drifting: relativedelta(months=k) applied fresh to
    Jan 31 each time correctly clamps to Feb 28, Mar 31, Apr 30, ... Iteratively
    adding 1 month to the previous *result* instead would drift permanently to the
    28th after the first February."""
    step = recurring.interval * n
    F = RecurringTransaction.Frequency
    if recurring.frequency == F.WEEKLY:
        return recurring.start_date + relativedelta(weeks=step)
    if recurring.frequency == F.MONTHLY:
        return recurring.start_date + relativedelta(months=step)
    if recurring.frequency == F.QUARTERLY:
        return recurring.start_date + relativedelta(months=step * 3)
    if recurring.frequency == F.YEARLY:
        return recurring.start_date + relativedelta(years=step)
    raise ValueError(f'Unknown frequency: {recurring.frequency}')


@db_transaction.atomic
def ensure_recurring_horizon(recurring, horizon_months=DEFAULT_HORIZON_MONTHS, today=None):
    """Idempotently materialize Transaction rows for `recurring` through
    today + horizon_months. Returns the count of newly-created rows.

    generated_until is a cache of "the horizon target date as of the last
    successful run" — the fast-path below is a single field comparison with no
    DB writes once a template is already generated far enough ahead for today.

    When the cache misses, generation always walks from occurrence #0 (the
    start_date anchor), not from a cached "resume index". This is deliberately
    NOT an optimization for the common case — it's what makes the function
    self-healing: get_or_create no-ops on dates that already have a row
    (executed, overdue-unexecuted, or already-generated future ones) and only
    creates the dates that are actually missing, regardless of *why* they're
    missing (executed out of chronological order, a manually deleted
    mid-sequence occurrence, or start_date/frequency having just changed so the
    old row count no longer means anything against the new schedule). A
    resume-index approach was tried first and broken by exactly those cases —
    walking the full anchor-relative sequence every time the cache misses is
    the fix, not a micro-optimization to reintroduce carefully.
    """
    today = today or timezone.localdate()
    horizon_date = today + relativedelta(months=horizon_months)
    if not recurring.is_active:
        return 0
    if recurring.generated_until and recurring.generated_until >= horizon_date:
        return 0

    created = 0
    n = 0
    while True:
        occ_date = _occurrence_date(recurring, n)
        if occ_date > horizon_date:
            break
        if recurring.end_date and occ_date > recurring.end_date:
            break
        _, was_created = Transaction.objects.get_or_create(
            recurring_source=recurring, due_date=occ_date,
            defaults=dict(account=recurring.account, category=recurring.category,
                          direction=recurring.direction, amount=recurring.amount,
                          description=recurring.description, executed=False),
        )
        created += int(was_created)
        n += 1

    recurring.generated_until = horizon_date
    recurring.save(update_fields=['generated_until'])
    return created


def ensure_recurring_horizon_for_all_active(horizon_months=DEFAULT_HORIZON_MONTHS, today=None):
    today = today or timezone.localdate()
    return {
        r.pk: ensure_recurring_horizon(r, horizon_months=horizon_months, today=today)
        for r in RecurringTransaction.objects.filter(is_active=True)
    }


@db_transaction.atomic
def regenerate_future_occurrences(recurring, today=None):
    """Called after editing a template's amount/schedule/etc. Deletes and
    regenerates only the UNEXECUTED occurrences with due_date >= today; executed
    history and overdue-unexecuted rows (due_date < today) are left untouched."""
    today = today or timezone.localdate()
    recurring.occurrences.filter(executed=False, due_date__gte=today).delete()
    recurring.generated_until = None
    recurring.save(update_fields=['generated_until'])
    return ensure_recurring_horizon(recurring, today=today)


@db_transaction.atomic
def deactivate_recurring(recurring, today=None):
    today = today or timezone.localdate()
    recurring.occurrences.filter(executed=False, due_date__gte=today).delete()
    recurring.is_active = False
    # Reset the horizon cache too: if this template is ever reactivated by ANY
    # path (the app's edit form, admin, a shell) rather than only the one flow
    # that happens to also call regenerate_future_occurrences, the next
    # ensure_recurring_horizon call must not short-circuit on a stale cache
    # from before deactivation and silently generate nothing.
    recurring.generated_until = None
    recurring.save(update_fields=['is_active', 'generated_until'])


@db_transaction.atomic
def delete_recurring(recurring, today=None):
    """Hard-delete the template. Future unexecuted occurrences are removed
    first; any remaining linked rows (executed, or overdue-unexecuted) survive
    with recurring_source set to NULL via on_delete=SET_NULL."""
    today = today or timezone.localdate()
    recurring.occurrences.filter(executed=False, due_date__gte=today).delete()
    recurring.delete()
