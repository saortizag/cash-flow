from django.contrib import admin, messages

from . import services
from .models import Account, Category, CreditCardStatement, RecurringTransaction, Transaction, Transfer


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'account_type', 'current_balance', 'is_active', 'cut_day', 'payment_due_day')
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
        if obj and self._is_transfer_leg(obj):
            # A transfer leg's fields are only ever safe to change in lockstep
            # with its paired leg (services.update_transfer). Editing one side
            # here — even just description/due_date — would desync the pair.
            readonly += ['account', 'direction', 'amount', 'category', 'description', 'due_date']
        return readonly

    def has_delete_permission(self, request, obj=None):
        if obj is not None and (obj.executed or self._is_transfer_leg(obj)):
            return False
        return super().has_delete_permission(request, obj)

    @staticmethod
    def _is_transfer_leg(obj):
        return hasattr(obj, 'transfer_as_source') or hasattr(obj, 'transfer_as_destination')


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    """Deliberately view/delete-only, no add or change form: the default admin
    add/change UI would show out_leg/in_leg as raw FK pickers, letting someone
    repoint a Transfer at unrelated Transaction rows and corrupt the pairing.
    Use the app's own Transfers pages to create/edit; deletion here still
    routes through services.delete_transfer so both legs are removed together."""
    list_display = ('id', 'out_leg', 'in_leg', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is not None and (obj.out_leg.executed or obj.in_leg.executed):
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        services.delete_transfer(obj)

    def delete_queryset(self, request, queryset):
        # All-or-nothing: check every selected transfer up front rather than
        # deleting one-by-one and letting services.delete_transfer's
        # ValidationError abort the loop partway through, which would leave
        # some transfers deleted and others not with no clear explanation.
        blocked = [t for t in queryset if t.out_leg.executed or t.in_leg.executed]
        if blocked:
            self.message_user(
                request,
                f'Cannot delete: {len(blocked)} of the selected transfer(s) are executed. '
                'Un-execute them first, or deselect them and retry.',
                level=messages.ERROR,
            )
            return
        for obj in queryset:
            services.delete_transfer(obj)


@admin.register(CreditCardStatement)
class CreditCardStatementAdmin(admin.ModelAdmin):
    """View/delete-only, same reasoning as TransferAdmin: statements are
    created by services.bootstrap_statement/close_statement_if_due, which also
    claim the relevant Transaction rows and (for real cycles) create the
    linked payment-obligation Transfer — a raw admin edit could desync all of
    that. Viewing is still useful for troubleshooting."""
    list_display = ('account', 'cut_date', 'due_date', 'statement_balance', 'is_paid')
    list_filter = ('account',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


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
