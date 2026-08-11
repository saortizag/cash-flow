from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Account(models.Model):
    class AccountType(models.TextChoices):
        CASH = 'cash', 'Cash'
        CHECKING = 'checking', 'Bank Checking'
        SAVINGS = 'savings', 'Bank Savings'
        CREDIT_CARD = 'credit_card', 'Credit Card'
        OTHER = 'other', 'Other'

    name = models.CharField(max_length=100, unique=True)
    account_type = models.CharField(max_length=20, choices=AccountType.choices, default=AccountType.CASH)
    current_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    is_active = models.BooleanField(default=True)
    # Credit-card-only fields (nullable, meaningless for other account types). Kept on the shared
    # model rather than a one-to-one profile so card logic never needs an extra join; the cost is
    # a few always-null columns on non-card accounts, fine at personal-app scale. For a credit
    # card, current_balance is a NEGATIVE convention ("-450.00" = owe 450) — purchases are
    # ordinary OUT transactions and payments are ordinary IN transactions, needing zero changes to
    # signed_amount/execute_transaction/unexecute_transaction to behave correctly.
    cut_day = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text='Day of month the statement closes (fecha de corte). Credit cards only.',
    )
    payment_due_day = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text='Day of the following month payment is due. Credit cards only.',
    )
    next_statement_cut_date = models.DateField(
        null=True, blank=True, editable=False,
        help_text='System-managed: the next date a statement will close for this card.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Category(models.Model):
    class TypicalDirection(models.TextChoices):
        IN = 'IN', 'Income'
        OUT = 'OUT', 'Expense'
        BOTH = 'BOTH', 'Both / Either'

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    typical_direction = models.CharField(max_length=4, choices=TypicalDirection.choices, default=TypicalDirection.BOTH)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class RecurringTransaction(models.Model):
    class Frequency(models.TextChoices):
        WEEKLY = 'weekly', 'Weekly'
        MONTHLY = 'monthly', 'Monthly'
        QUARTERLY = 'quarterly', 'Quarterly'
        YEARLY = 'yearly', 'Yearly'

    class Direction(models.TextChoices):
        IN = 'IN', 'Income (money in)'
        OUT = 'OUT', 'Expense (money out)'

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='recurring_transactions')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='recurring_transactions')
    direction = models.CharField(max_length=3, choices=Direction.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    description = models.CharField(max_length=255, blank=True)
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    interval = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text='Repeat every N periods, e.g. frequency=weekly + interval=2 = biweekly',
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text='Leave blank for an indefinite schedule.')
    is_active = models.BooleanField(default=True)
    generated_until = models.DateField(
        null=True, blank=True, editable=False,
        help_text='System-managed: the horizon date through which occurrences have been generated.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', 'start_date']
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='recurring_amount_positive'),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True) | models.Q(end_date__gte=models.F('start_date')),
                name='recurring_end_date_after_start_date',
            ),
        ]

    def __str__(self):
        return f'{self.description or self.get_direction_display()} ({self.get_frequency_display()})'


class Transaction(models.Model):
    class Direction(models.TextChoices):
        IN = 'IN', 'Income (money in)'
        OUT = 'OUT', 'Expense (money out)'

    # Nullable: a Transaction can be a "planned" future obligation with no funding account
    # decided yet (assigned later via services.assign_account), or a transfer leg whose source
    # isn't chosen yet (e.g. an auto-generated credit card payment obligation). executed=True can
    # never coexist with account=None — see the CheckConstraint below; you can't actually pay from
    # nowhere.
    account = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, blank=True, related_name='transactions')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    direction = models.CharField(max_length=3, choices=Direction.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    description = models.CharField(max_length=255, blank=True)
    due_date = models.DateField()
    executed = models.BooleanField(default=False)
    executed_date = models.DateField(null=True, blank=True)
    recurring_source = models.ForeignKey(RecurringTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='occurrences')
    # Forward reference (string) to CreditCardStatement, defined later in this file — which credit
    # card expense was claimed into which statement, set by services.close_statement_if_due.
    statement = models.ForeignKey('CreditCardStatement', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='claimed_expenses')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_date', 'id']
        indexes = [
            models.Index(fields=['due_date']),
            models.Index(fields=['executed']),
            models.Index(fields=['account', 'executed', 'due_date']),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='transaction_amount_positive'),
            models.CheckConstraint(
                condition=(models.Q(executed=False, executed_date__isnull=True)
                           | models.Q(executed=True, executed_date__isnull=False)),
                name='transaction_executed_date_consistency',
            ),
            models.CheckConstraint(
                condition=models.Q(executed=False) | models.Q(account__isnull=False),
                name='transaction_executed_requires_account',
            ),
            models.UniqueConstraint(fields=['recurring_source', 'due_date'],
                                     name='unique_occurrence_per_template_per_date'),
        ]

    def __str__(self):
        acct = self.account.name if self.account_id else 'Unassigned'
        return f'{self.due_date} {self.get_direction_display()} {self.amount} ({acct})'


class Transfer(models.Model):
    """Two linked Transaction rows (an OUT leg on the source, an IN leg on the destination),
    always created/executed/un-executed/deleted together. Deliberately NOT a standalone model
    with its own balance logic — see the plan notes: this way transfers flow through every
    existing Transaction query (transaction_list, dashboard, projection) with zero new query
    logic, since they're just Transactions that happen to be linked."""

    out_leg = models.OneToOneField(Transaction, on_delete=models.PROTECT, related_name='transfer_as_source')
    in_leg = models.OneToOneField(Transaction, on_delete=models.PROTECT, related_name='transfer_as_destination')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        source = self.out_leg.account.name if self.out_leg.account_id else 'Unassigned'
        dest = self.in_leg.account.name if self.in_leg.account_id else 'Unassigned'
        return f'{source} → {dest}: {self.out_leg.amount}'


class CreditCardStatement(models.Model):
    """A closed (or manually bootstrapped) billing cycle for a credit card: a snapshot of what's
    owed and by when. Not a live-computed figure — see services.close_statement_if_due for why
    membership is explicit (a `statement` FK claimed onto Transaction rows) rather than derived
    from a date-range query or a snapshot of the live running balance."""

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='statements')
    # Null for a manually-entered bootstrap statement with no real cycle behind it yet.
    cut_date = models.DateField(null=True, blank=True)
    due_date = models.DateField()
    # Positive "amount owed" — human-facing, the OPPOSITE convention from Account.current_balance
    # (which is negative for a card). 0 is valid: a cycle where refunds offset purchases.
    statement_balance = models.DecimalField(max_digits=10, decimal_places=2,
                                             validators=[MinValueValidator(Decimal('0.00'))])
    payment_obligation = models.OneToOneField(Transfer, on_delete=models.SET_NULL, null=True, blank=True,
                                               related_name='credit_card_statement')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-due_date']

    @property
    def is_paid(self):
        return bool(self.payment_obligation_id and self.payment_obligation.in_leg.executed)

    def __str__(self):
        return f'{self.account.name} statement due {self.due_date}: {self.statement_balance}'
