from decimal import Decimal

from django.core.validators import MinValueValidator
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

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='transactions')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    direction = models.CharField(max_length=3, choices=Direction.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    description = models.CharField(max_length=255, blank=True)
    due_date = models.DateField()
    executed = models.BooleanField(default=False)
    executed_date = models.DateField(null=True, blank=True)
    recurring_source = models.ForeignKey(RecurringTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='occurrences')
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
            models.UniqueConstraint(fields=['recurring_source', 'due_date'],
                                     name='unique_occurrence_per_template_per_date'),
        ]

    def __str__(self):
        return f'{self.due_date} {self.get_direction_display()} {self.amount} ({self.account})'
