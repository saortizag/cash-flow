from decimal import Decimal

from django.test import SimpleTestCase

from ledger.templatetags.ledger_extras import money


class MoneyFilterTests(SimpleTestCase):
    def test_formats_with_dollar_sign_and_commas(self):
        self.assertEqual(money(Decimal('2785154.00')), '$2,785,154.00')

    def test_negative_value_keeps_sign_before_dollar_sign(self):
        self.assertEqual(money(Decimal('-1954395.00')), '-$1,954,395.00')

    def test_small_value_no_thousands_separator_needed(self):
        self.assertEqual(money(Decimal('70.50')), '$70.50')

    def test_zero(self):
        self.assertEqual(money(Decimal('0.00')), '$0.00')

    def test_none_is_blank(self):
        self.assertEqual(money(None), '')

    def test_non_numeric_value_passed_through_unchanged(self):
        self.assertEqual(money('not a number'), 'not a number')
