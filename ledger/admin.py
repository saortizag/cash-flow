from django.contrib import admin

from . import services
from .models import Account, Category, RecurringTransaction, Transaction


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'account_type', 'current_balance', 'is_active')
    list_filter = ('account_type', 'is_active')
    search_fields = ('name',)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'typical_direction')
    search_fields = ('name',)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('due_date', 'account', 'direction', 'amount', 'category', 'executed', 'executed_date')
    list_filter = ('executed', 'direction', 'account', 'category')
    date_hierarchy = 'due_date'

    def get_readonly_fields(self, request, obj=None):
        # `executed`/`executed_date` are ALWAYS readonly here — flipping execution
        # status must go through services.execute_transaction/unexecute_transaction
        # (via the app's execute/unexecute views) so Account.current_balance stays
        # in sync. Editing them directly in admin would silently desync the balance.
        readonly = ['executed', 'executed_date']
        if obj and obj.executed:
            readonly += ['account', 'direction', 'amount']
        return readonly

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.executed:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(RecurringTransaction)
class RecurringTransactionAdmin(admin.ModelAdmin):
    list_display = ('description', 'account', 'direction', 'amount', 'frequency', 'interval',
                     'is_active', 'generated_until')
    list_filter = ('is_active', 'frequency', 'account')
    readonly_fields = ('generated_until',)

    def save_model(self, request, obj, form, change):
        # Route through services so admin edits keep materialized occurrences
        # in sync exactly like the app's own recurring_update view does —
        # otherwise an amount/schedule change here would silently leave
        # already-generated future occurrences stale.
        super().save_model(request, obj, form, change)
        if obj.is_active:
            services.regenerate_future_occurrences(obj)
        else:
            services.deactivate_recurring(obj)

    def delete_model(self, request, obj):
        services.delete_recurring(obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            services.delete_recurring(obj)
