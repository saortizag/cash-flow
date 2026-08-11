import django_filters

from ledger.models import Transaction


class TransactionFilter(django_filters.FilterSet):
    """Mirrors ledger.views.transaction_list's GET-param filtering exactly,
    including the special account=unassigned value for pending transactions
    with no account assigned yet."""

    account = django_filters.CharFilter(method='filter_account')
    executed = django_filters.BooleanFilter()
    direction = django_filters.ChoiceFilter(choices=Transaction.Direction.choices)

    class Meta:
        model = Transaction
        fields = ['account', 'category', 'executed', 'direction']

    def filter_account(self, queryset, name, value):
        if value == 'unassigned':
            return queryset.filter(account__isnull=True)
        if value.isdigit():
            return queryset.filter(account_id=value)
        return queryset.none()
