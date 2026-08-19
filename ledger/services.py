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

import os
from datetime import timedelta
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


def _delete_attachment_file(field_file):
    """Removes the file AND its now-empty per-upload directory (see
    models.attachment_upload_path — each upload gets a fresh directory holding exactly one
    file, so removing that directory once its file is gone is always safe, never a race with
    a sibling). Assumes local FileSystemStorage (field_file.path), consistent with this app's
    local-first design — see cash.settings' MEDIA_ROOT comment."""
    directory = os.path.dirname(field_file.path)
    field_file.delete(save=False)
    try:
        os.rmdir(directory)
    except OSError:
        pass  # not empty (shouldn't happen) or already gone — either way, not fatal


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
                        executed=False, executed_date=None, recurring_source=None, attachment=None):
    """Create a Transaction. If executed=True (logging something that already
    happened), apply the balance effect atomically as part of the same call."""
    txn = Transaction.objects.create(
        account=account, category=category, direction=direction, amount=amount,
        description=description, due_date=due_date, recurring_source=recurring_source,
        attachment=attachment,
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


def update_transaction_attachment(txn, attachment):
    """Set/replace (attachment=an uploaded file) or clear (attachment=None) the optional
    support document. Always allowed regardless of executed state — same rationale as
    update_transaction_open_fields, and deliberately its own function rather than a parameter
    on that one: callers here always pass an unambiguous value (a file, or None), unlike a
    Django form's cleaned_data where a FileField's None/False/file tri-state ("no change" vs
    "clear" vs "replace") needs translating before it gets here — keeping that translation in
    the view/form layer, not this one, matching how AttachmentForm is the single place that
    ambiguity gets resolved.

    Deletes the PREVIOUS file from storage (not just the DB pointer) once the new value has
    committed — FileField does not do this on save() on its own, and leaving it out would leak
    a physical file on every replace/clear, forever. on_commit rather than an immediate delete:
    if the surrounding request is itself inside a transaction that later rolls back, the file
    must not vanish out from under a row that (after rollback) still points at it."""
    old = txn.attachment
    txn.attachment = attachment
    txn.save(update_fields=['attachment', 'updated_at'])
    if old:
        db_transaction.on_commit(lambda: _delete_attachment_file(old))
    return txn


@db_transaction.atomic
def delete_transaction(txn):
    locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if locked_txn.executed:
        raise ValidationError('Cannot delete an executed transaction. Un-execute it first.')
    attachment = locked_txn.attachment
    locked_txn.delete()
    if attachment:
        db_transaction.on_commit(lambda: _delete_attachment_file(attachment))


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
                     executed=False, executed_date=None, attachment=None):
    out_leg = Transaction.objects.create(account=from_account, direction=Transaction.Direction.OUT,
                                          amount=amount, description=description, due_date=due_date,
                                          attachment=attachment)
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


def update_transfer_attachment(transfer, attachment):
    """Stored on out_leg — see Transfer.attachment property. Same always-allowed-regardless-
    of-executed-state rationale as update_transaction_attachment; deliberately NOT part of
    update_transfer, which is reserved for the financial fields and stays guarded by the
    executed check (attaching a receipt after the fact shouldn't require un-executing first)."""
    return update_transaction_attachment(transfer.out_leg, attachment)


@db_transaction.atomic
def delete_transfer(transfer):
    out_leg = Transaction.objects.select_for_update().get(pk=transfer.out_leg_id)
    in_leg = Transaction.objects.select_for_update().get(pk=transfer.in_leg_id)
    if out_leg.executed or in_leg.executed:
        raise ValidationError('Cannot delete an executed transfer. Un-execute it first.')
    attachment = out_leg.attachment  # see update_transaction_attachment for why on_commit
    transfer.delete()   # delete the Transfer row first — its PROTECT references are what
    out_leg.delete()    # stop the legs from being deleted directly (by design, to block
    in_leg.delete()     # accidentally orphaning one side); once it's gone, legs delete freely.
    if attachment:
        db_transaction.on_commit(lambda: _delete_attachment_file(attachment))


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


# ---------- Reporting (read-only aggregation) ----------
# Pure read functions, no mutation — the single source of truth for the
# dashboard/projection numbers, shared between the ledger templates and the
# api app so the two surfaces can never silently disagree on the arithmetic.

def account_summary(today=None):
    """Assets / card debt / net worth across active accounts (credit-card
    negative-balance convention — see Account docstring), plus overdue,
    upcoming (capped at 10), and unassigned pending transactions."""
    today = today or timezone.localdate()
    accounts = Account.objects.filter(is_active=True)
    asset_accounts = [a for a in accounts if a.account_type != Account.AccountType.CREDIT_CARD]
    card_accounts = [a for a in accounts if a.account_type == Account.AccountType.CREDIT_CARD]
    total_assets = sum((a.current_balance for a in asset_accounts), start=Decimal('0.00'))
    total_card_debt = -sum((a.current_balance for a in card_accounts), start=Decimal('0.00'))
    net_worth = total_assets - total_card_debt

    # transfer_as_source/transfer_as_destination are select_related (not
    # prefetch_related) even though they're reverse relations — Django
    # supports this for O2O specifically, since it's still a single-row join.
    # Needed so API serialization of these transactions (is_transfer_leg)
    # doesn't trigger up to two extra queries per row.
    overdue = Transaction.objects.filter(
        executed=False, due_date__lt=today, account__isnull=False,
    ).select_related('account', 'category', 'transfer_as_source', 'transfer_as_destination')
    upcoming = Transaction.objects.filter(
        executed=False, due_date__gte=today, account__isnull=False,
    ).select_related('account', 'category', 'transfer_as_source', 'transfer_as_destination')[:10]
    unassigned = Transaction.objects.filter(
        executed=False, account__isnull=True,
    ).select_related('category', 'transfer_as_source', 'transfer_as_destination').order_by('due_date')

    return {
        'accounts': accounts,
        'total_assets': total_assets,
        'total_card_debt': total_card_debt,
        'net_worth': net_worth,
        'overdue': overdue,
        'upcoming': upcoming,
        'unassigned': unassigned,
    }


def project_balances(target_date, account=None):
    """Per-account (or single `account`, if given) running balance walk to
    target_date: starts from current_balance and applies signed_amount for
    every unexecuted transaction due by target_date, in chronological order.
    Also returns combined current/projected totals. Unassigned pending
    obligations are only folded in (as their own rows/total, added to
    combined_projected) when account is None — attributing them to one
    specific account's projection would be arbitrary."""
    accounts = [account] if account else list(Account.objects.filter(is_active=True))

    results = []
    for acct in accounts:
        pending = Transaction.objects.filter(
            account=acct, executed=False, due_date__lte=target_date,
        ).select_related('category', 'transfer_as_source', 'transfer_as_destination').order_by('due_date', 'id')
        running = acct.current_balance
        rows = []
        for txn in pending:
            running += signed_amount(txn.direction, txn.amount)
            rows.append({'transaction': txn, 'running_balance': running})
        results.append({
            'account': acct,
            'current_balance': acct.current_balance,
            'projected_balance': running,
            'rows': rows,
        })

    combined_current = sum((r['current_balance'] for r in results), Decimal('0.00'))
    combined_projected = sum((r['projected_balance'] for r in results), Decimal('0.00'))

    # See project_balances docstring: unassigned obligations belong to no
    # specific account, so they only ever factor into the "all accounts
    # combined" view, never a single-account one.
    unassigned_rows = []
    unassigned_total = Decimal('0.00')
    if account is None:
        unassigned_pending = Transaction.objects.filter(
            account__isnull=True, executed=False, due_date__lte=target_date,
        ).select_related('category', 'transfer_as_source', 'transfer_as_destination').order_by('due_date', 'id')
        for txn in unassigned_pending:
            unassigned_total += signed_amount(txn.direction, txn.amount)
            unassigned_rows.append({'transaction': txn, 'running_total': unassigned_total})
        combined_projected += unassigned_total

    return {
        'results': results,
        'target_date': target_date,
        'combined_current': combined_current,
        'combined_projected': combined_projected,
        'unassigned_rows': unassigned_rows,
        'unassigned_total': unassigned_total,
    }


# ---------- Expense chart ----------
# Trailing-window size per resolution — chosen so each chart stays readable (30 bars max) rather
# than growing unbounded with account age.
EXPENSE_CHART_WINDOWS = {'day': 30, 'week': 12, 'month': 12}


def _short_date_label(d):
    return f'{d.strftime("%b")} {d.day}'


def _month_label(d):
    return f'{d.strftime("%b")} {d.year}'


# Bucket keys are the period's start date (a Transaction's executed_date rounds down to it);
# steps walk one bucket further into the past. Deliberately plain Python date math, not
# TruncDay/TruncWeek/TruncMonth — the bucket boundary is defined in exactly one place we
# control and can test precisely, rather than depending on a DB function's week-start/timezone
# behavior (see the recurring-generation and cut-date helpers above for the same preference).
_EXPENSE_CHART_BUCKETS = {
    'day': (lambda d: d, lambda d: d - timedelta(days=1), _short_date_label),
    'week': (lambda d: d - timedelta(days=d.weekday()), lambda d: d - timedelta(weeks=1), _short_date_label),
    'month': (lambda d: d.replace(day=1), lambda d: d - relativedelta(months=1), _month_label),
}


def expense_totals_by_period(resolution, today=None):
    """Total executed, OUT-direction spending per period for the trailing window
    (EXPENSE_CHART_WINDOWS), oldest first, ending at today's own bucket. Zero-filled for periods
    with no spend so bar spacing stays uniform even across a gap.

    Grouped by executed_date (when the money actually left), not due_date — this is a historical
    spending chart, not a plan, and the two can differ (a bill's due_date vs. the day it was
    actually paid).

    Transfer legs are excluded (transfer_as_source__isnull=True): a transfer's OUT leg moves
    money between the user's own accounts, it isn't spending — counting it would also
    double-count a credit card payment on top of the purchases it settles, since the purchases
    were already counted as ordinary OUT transactions on the card."""
    if resolution not in EXPENSE_CHART_WINDOWS:
        raise ValueError(f'Unknown resolution: {resolution}')
    today = today or timezone.localdate()
    bucket_of, step_back, label_of = _EXPENSE_CHART_BUCKETS[resolution]

    periods = []
    cursor = bucket_of(today)
    for _ in range(EXPENSE_CHART_WINDOWS[resolution]):
        periods.append(cursor)
        cursor = step_back(cursor)
    periods.reverse()

    totals = dict.fromkeys(periods, Decimal('0.00'))
    rows = Transaction.objects.filter(
        executed=True, direction=Transaction.Direction.OUT, transfer_as_source__isnull=True,
        executed_date__gte=periods[0],
    ).values_list('executed_date', 'amount')
    for executed_date, amount in rows:
        totals[bucket_of(executed_date)] += amount

    return [{'date': period, 'label': label_of(period), 'total': totals[period]} for period in periods]
