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

from .models import Account, CreditCardStatement, RecurringTransaction, Transaction, Transfer

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
    if locked_txn.account_id is None:
        raise ValidationError('Assign an account before executing this transaction.')
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


# ---------- Unassigned-account transactions ----------

@db_transaction.atomic
def assign_account(txn, account):
    """The only sanctioned way to attach an account to a previously-unassigned
    pending Transaction (a plain planned payment, or a transfer's out_leg).
    Locks+rechecks fresh state before checking executed/account_id, matching
    every other check-then-act function in this file — two concurrent
    assign-account submissions for the same row must not let the second
    silently overwrite the first with no error."""
    locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if locked_txn.executed:
        raise ValidationError('Cannot assign an account to an already-executed transaction.')
    if locked_txn.account_id is not None:
        raise ValidationError('This transaction already has an account assigned.')
    locked_txn.account = account
    locked_txn.save(update_fields=['account', 'updated_at'])
    txn.account = locked_txn.account
    return txn


# ---------- Transfers ----------
# A Transfer is two linked Transaction rows (an OUT leg on the source, an IN
# leg on the destination) rather than a standalone balance-affecting model —
# see Transfer's docstring in models.py for why. Both legs always move
# together: create/execute/unexecute/update/delete all wrap both legs in one
# outer atomic block so it's never possible to end up with only one side done.

@db_transaction.atomic
def create_transfer(*, from_account, to_account, amount, description, due_date,
                     executed=False, executed_date=None):
    out_leg = Transaction.objects.create(account=from_account, direction=Transaction.Direction.OUT,
                                          amount=amount, description=description, due_date=due_date)
    in_leg = Transaction.objects.create(account=to_account, direction=Transaction.Direction.IN,
                                         amount=amount, description=description, due_date=due_date)
    transfer = Transfer.objects.create(out_leg=out_leg, in_leg=in_leg)
    if executed:
        execute_transfer(transfer, executed_date=executed_date)
    return transfer


@db_transaction.atomic
def execute_transfer(transfer, executed_date=None):
    if transfer.out_leg.account_id is None:
        raise ValidationError('Assign a source account before executing this transfer.')
    execute_transaction(transfer.out_leg, executed_date=executed_date)
    execute_transaction(transfer.in_leg, executed_date=executed_date)
    return transfer


@db_transaction.atomic
def unexecute_transfer(transfer):
    # Same out_leg-then-in_leg order as execute_transfer/update_transfer/
    # delete_transfer — every function touching a transfer's two legs must
    # lock them in the SAME order, or two of these running concurrently on
    # the same transfer (e.g. a double form submit) could deadlock.
    unexecute_transaction(transfer.out_leg)
    unexecute_transaction(transfer.in_leg)
    return transfer


@db_transaction.atomic
def update_transfer(transfer, *, amount, description, due_date):
    """Amount/description/due_date only, updated on both legs in lockstep.
    Only valid while unexecuted. Reassigning accounts on an already-linked
    transfer isn't supported — delete and recreate covers that rare case."""
    for leg_id in (transfer.out_leg_id, transfer.in_leg_id):
        locked = Transaction.objects.select_for_update().get(pk=leg_id)
        if locked.executed:
            raise ValidationError('Cannot edit an executed transfer. Un-execute it first.')
        locked.amount = amount
        locked.description = description
        locked.due_date = due_date
        locked.save(update_fields=['amount', 'description', 'due_date', 'updated_at'])
    return transfer


@db_transaction.atomic
def delete_transfer(transfer):
    out_leg = Transaction.objects.select_for_update().get(pk=transfer.out_leg_id)
    in_leg = Transaction.objects.select_for_update().get(pk=transfer.in_leg_id)
    if out_leg.executed or in_leg.executed:
        raise ValidationError('Cannot delete an executed transfer. Un-execute it first.')
    transfer.delete()   # delete the Transfer row first — its PROTECT references are what
    out_leg.delete()    # stop the legs from being deleted directly (by design, to block
    in_leg.delete()     # accidentally orphaning one side); once it's gone, legs delete freely.


# ---------- Credit card statement cycling ----------

def _next_month_day(from_date, day):
    """The date with day-of-month `day` in the month immediately after
    from_date's month, clamped to that month's actual length (e.g. day=31
    applied to a 30-day month lands on its 30th) — the same relativedelta
    clamping behavior _occurrence_date already relies on above. Used for
    computing a due_date from a real cut_date, and for advancing
    next_statement_cut_date after a real close — both of those genuinely
    always mean "one month later," never "later this month."""
    return from_date + relativedelta(months=1, day=day)


def _next_occurrence_of_day(from_date, day):
    """The NEAREST date with day-of-month `day` strictly after from_date —
    later this month if that day hasn't happened yet, otherwise next month.
    Distinct from _next_month_day (which always jumps a full month): this one
    is specifically for SEEDING a card's first next_statement_cut_date (at
    bootstrap or self-heal time), where "always next month" would wrongly
    skip an upcoming same-month cut day that hasn't happened yet — e.g.
    configuring a card on the 11th with cut_day=25 should seed to the 25th of
    THIS month, not next month. "from_date == day" counts as already passed
    (seeds to next month), same as any other date on/before from_date."""
    candidate = from_date + relativedelta(day=day)
    if candidate <= from_date:
        candidate = from_date + relativedelta(months=1, day=day)
    return candidate


@db_transaction.atomic
def bootstrap_statement(card, amount_owed, due_date, today=None):
    """'I owe X, due on D' — a manually-entered statement with no itemized
    history behind it, for onboarding a card that already has a balance. Sets
    the card's live balance to match and seeds next_statement_cut_date so
    future cycling starts cleanly from here without re-claiming anything this
    figure already covers.

    Also claims any already-executed, not-yet-claimed Transaction rows on the
    card into this statement (without adding their amounts to
    statement_balance, which is the user's own figure). Without this, a
    purchase logged BEFORE bootstrapping — presumably already reflected in
    the real-world amount_owed the user just typed in — would still have
    statement=NULL and get folded into (and double-counted by) the next real
    close_statement_if_due."""
    today = today or timezone.localdate()
    statement = CreditCardStatement.objects.create(
        account=card, cut_date=None, due_date=due_date, statement_balance=amount_owed,
    )
    Transaction.objects.filter(account=card, executed=True, statement__isnull=True).update(statement=statement)
    card.current_balance = -amount_owed
    if card.cut_day and card.payment_due_day:
        card.next_statement_cut_date = _next_occurrence_of_day(today, card.cut_day)
    card.save(update_fields=['current_balance', 'next_statement_cut_date', 'updated_at'])
    return statement


@db_transaction.atomic
def _claim_due_cut_date(card, today):
    """Locks the Account row, checks whether a cycle is due, and — if so —
    immediately advances next_statement_cut_date and commits before doing any
    Transaction-touching work. This is its own short, self-contained
    transaction (not folded into close_statement_if_due's larger one) so the
    Account lock is never held while claimant Transaction rows are locked —
    execute_transaction/unexecute_transaction lock Transaction-then-Account,
    so holding Account-then-Transaction here for the whole close would risk a
    lock-order deadlock against a concurrent purchase execution on the same
    card. Returns the cut_date that was claimed, or None if nothing was due.

    Trade-off: if the process crashes between this committing and
    close_statement_if_due finishing the materialization below, the claimed
    cycle is lost (no statement gets created for it, and it won't be retried
    since next_statement_cut_date has already moved on). Accepted as an
    unlikely failure mode for a personal app — the alternative (one long lock
    spanning both phases) reintroduces the deadlock risk this split exists to
    avoid."""
    card = Account.objects.select_for_update().get(pk=card.pk)
    if not card.next_statement_cut_date or today < card.next_statement_cut_date:
        return None
    if not card.payment_due_day:
        # Cycling is paused until payment_due_day is (re)configured — without
        # it there's no valid day to compute a due_date from. Deliberately
        # does NOT advance next_statement_cut_date, so this is retried on
        # every future check rather than silently skipping a cycle forever.
        return None
    cut_date = card.next_statement_cut_date
    card.next_statement_cut_date = _next_month_day(cut_date, card.cut_day)
    card.save(update_fields=['next_statement_cut_date', 'updated_at'])
    return cut_date


def close_statement_if_due(card, today=None):
    """Idempotent, self-healing statement close — called opportunistically
    (dashboard/projection views), same pattern as
    ensure_recurring_horizon_for_all_active. May run well after the real
    cut_date has passed, by which point new post-cut purchases may already
    exist — so membership is explicit (Transaction.statement, claimed here)
    rather than a live current_balance snapshot, which would wrongly absorb
    those newer purchases into the closing cycle regardless of when this
    happens to run. Returns the new statement, or None if nothing was due.

    Deliberately NOT wrapped in one @db_transaction.atomic: it calls
    _claim_due_cut_date and _materialize_statement, each independently
    atomic, as two SEPARATE top-level transactions. If this function itself
    were atomic, calling _claim_due_cut_date from inside it would turn that
    inner atomic into a mere savepoint rather than a transaction that
    actually commits (and releases its Account lock) before the second phase
    starts — silently defeating the whole point of the split."""
    today = today or timezone.localdate()
    cut_date = _claim_due_cut_date(card, today)
    if cut_date is None:
        return None
    return _materialize_statement(card, cut_date)


@db_transaction.atomic
def _materialize_statement(card, cut_date):
    # Excludes transactions that are themselves the in_leg of a PAYMENT
    # transfer (i.e. transfer_as_destination -> credit_card_statement is
    # set) — a statement's payment obligation materializes as an ordinary IN
    # transaction on the card, so without this exclusion, paying an old
    # statement before the NEXT cut runs would let that payment get swept
    # into (and corrupt) the following statement's own claiming/sum, as if
    # it were a fresh credit reducing THAT cycle's debt instead of settling
    # the previous one.
    claimable = Transaction.objects.filter(
        account=card, executed=True, statement__isnull=True, due_date__lte=cut_date,
    ).exclude(transfer_as_destination__credit_card_statement__isnull=False)
    net_owed = -sum((signed_amount(t.direction, t.amount) for t in claimable), Decimal('0.00'))
    statement_balance = max(net_owed, Decimal('0.00'))
    due_date = _next_month_day(cut_date, card.payment_due_day)
    statement = CreditCardStatement.objects.create(
        account=card, cut_date=cut_date, due_date=due_date, statement_balance=statement_balance,
    )
    claimable.update(statement=statement)
    if statement_balance > 0:
        transfer = create_transfer(from_account=None, to_account=card, amount=statement_balance,
                                    description=f'{card.name} statement due {due_date}', due_date=due_date)
        statement.payment_obligation = transfer
        statement.save(update_fields=['payment_obligation'])
    return statement


def close_statements_if_due_for_all_cards(today=None):
    """Also self-heals next_statement_cut_date for a card whose cut_day/
    payment_due_day were set directly (e.g. via the plain Account form on a
    fresh $0 card) rather than through bootstrap_statement — the only other
    place that field gets seeded. Without this, such a card would look fully
    configured but silently never cycle, since close_statement_if_due only
    ever acts once next_statement_cut_date is non-null."""
    today = today or timezone.localdate()
    cards = Account.objects.filter(is_active=True, account_type=Account.AccountType.CREDIT_CARD)
    results = {}
    for card in cards:
        if not card.next_statement_cut_date and card.cut_day and card.payment_due_day:
            card.next_statement_cut_date = _next_occurrence_of_day(today, card.cut_day)
            card.save(update_fields=['next_statement_cut_date', 'updated_at'])
        results[card.pk] = close_statement_if_due(card, today=today)
    return results
