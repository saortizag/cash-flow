from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money(value):
    """Format a number as a human-readable amount: $1,234,567.89, or
    -$1,234,567.89 for negative values (e.g. a credit card's debt balance).
    Falls back to returning the value unchanged if it isn't numeric, so a
    stray non-numeric value in a template doesn't hard-crash the page."""
    if value is None or value == '':
        return ''
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign = '-' if value < 0 else ''
    return f'{sign}${abs(value):,.2f}'
