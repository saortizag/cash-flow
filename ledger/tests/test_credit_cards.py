from datetime import date
from decimal import Decimal

from django.test import TestCase

from ledger import services
from ledger.models import Account, CreditCardStatement, Transaction


class NextMonthDayTests(TestCase):
    def test_regular_month(self):
        self.assertEqual(services._next_month_day(date(2026, 1, 5), 20), date(2026, 2, 20))
        self.assertEqual(services._next_month_day(date(2026, 1, 20), 5), date(2026, 2, 5))

    def test_clamps_to_shorter_month(self):
        self.assertEqual(services._next_month_day(date(2026, 1, 31), 31), date(2026, 2, 28))  # 2026 not leap

    def test_year_rollover(self):
        self.assertEqual(services._next_month_day(date(2026, 12, 15), 31), date(2027, 1, 31))


class NextOccurrenceOfDayTests(TestCase):
    """_next_month_day always jumps a full month (right for advancing a REAL
    cycle forward); _next_occurrence_of_day finds the NEAREST upcoming
    occurrence (right for seeding a new card's first cut date) — these must
    diverge exactly when from_date is still before this month's day."""

    def test_day_not_yet_passed_this_month_stays_this_month(self):
        self.assertEqual(services._next_occurrence_of_day(date(2026, 8, 11), 25), date(2026, 8, 25))

    def test_day_already_passed_this_month_moves_to_next_month(self):
        self.assertEqual(services._next_occurrence_of_day(date(2026, 8, 11), 10), date(2026, 9, 10))

    def test_day_equal_to_from_date_moves_to_next_month(self):
        self.assertEqual(services._next_occurrence_of_day(date(2026, 8, 11), 11), date(2026, 9, 11))

    def test_clamps_to_shorter_month(self):
        self.assertEqual(services._next_occurrence_of_day(date(2026, 1, 5), 31), date(2026, 1, 31))
        self.assertEqual(services._next_occurrence_of_day(date(2026, 2, 5), 31), date(2026, 2, 28))


class CreditCardPurchaseTests(TestCase):
    """Purchases are ordinary Transactions on a credit-card account; the
    negative-balance convention means signed_amount/execute_transaction need
    no special-casing to get the direction of debt right."""

    def setUp(self):
        self.card = Account.objects.create(
            name='Visa', account_type=Account.AccountType.CREDIT_CARD, current_balance=Decimal('0.00'),
        )

    def test_purchase_increases_debt(self):
        txn = Transaction.objects.create(account=self.card, direction=Transaction.Direction.OUT,
                                          amount=Decimal('50.00'), due_date=date(2026, 1, 1))
        services.execute_transaction(txn)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal('-50.00'))

    def test_payment_reduces_debt(self):
        self.card.current_balance = Decimal('-100.00')
        self.card.save()
        credit = Transaction.objects.create(account=self.card, direction=Transaction.Direction.IN,
                                             amount=Decimal('40.00'), due_date=date(2026, 1, 1))
        services.execute_transaction(credit)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal('-60.00'))


class BootstrapStatementTests(TestCase):
    def setUp(self):
        self.card = Account.objects.create(
            name='Visa', account_type=Account.AccountType.CREDIT_CARD, current_balance=Decimal('0.00'),
            cut_day=15, payment_due_day=5,
        )

    def test_sets_balance_and_seeds_next_cut_date_this_month_if_not_yet_passed(self):
        # today (Feb 1) is BEFORE this month's cut_day (15), so the nearest
        # upcoming cut is THIS month, not next month.
        statement = services.bootstrap_statement(self.card, Decimal('300.00'), date(2026, 3, 5), today=date(2026, 2, 1))
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal('-300.00'))
        self.assertEqual(self.card.next_statement_cut_date, date(2026, 2, 15))
        self.assertEqual(statement.statement_balance, Decimal('300.00'))
        self.assertIsNone(statement.cut_date)
        self.assertIsNone(statement.payment_obligation)

    def test_seeds_next_cut_date_next_month_if_already_passed(self):
        # today (Feb 20) is AFTER this month's cut_day (15), so the nearest
        # upcoming cut is next month.
        services.bootstrap_statement(self.card, Decimal('300.00'), date(2026, 3, 5), today=date(2026, 2, 20))
        self.card.refresh_from_db()
        self.assertEqual(self.card.next_statement_cut_date, date(2026, 3, 15))

    def test_does_not_cause_double_counting_on_next_real_close(self):
        # today (Feb 1) is before cut_day (15), so bootstrap seeds the first
        # real cut to THIS month (Feb 15) — an empty cycle, since the
        # bootstrap figure already covers everything up to today. The
        # purchase below (due Mar 1) belongs to the FOLLOWING cycle (Mar 15).
        services.bootstrap_statement(self.card, Decimal('300.00'), date(2026, 3, 5), today=date(2026, 2, 1))
        txn = Transaction.objects.create(account=self.card, direction=Transaction.Direction.OUT,
                                          amount=Decimal('40.00'), due_date=date(2026, 3, 1))
        services.execute_transaction(txn, executed_date=date(2026, 3, 1))

        empty_cycle = services.close_statement_if_due(self.card, today=date(2026, 3, 20))
        self.assertEqual(empty_cycle.statement_balance, Decimal('0.00'))
        self.assertIsNone(empty_cycle.payment_obligation)

        statement = services.close_statement_if_due(self.card, today=date(2026, 3, 20))
        self.assertIsNotNone(statement)
        # Only the new purchase is claimed — the bootstrap figure isn't re-counted.
        self.assertEqual(statement.statement_balance, Decimal('40.00'))

    def test_claims_purchases_logged_before_bootstrap_so_they_are_not_recounted_later(self):
        # Regression: a purchase logged BEFORE bootstrapping is presumably
        # already reflected in the amount_owed the user typed in — it must
        # not still be sitting unclaimed, or the next real close would fold
        # it into a new statement and double-count it.
        pre_existing = Transaction.objects.create(account=self.card, direction=Transaction.Direction.OUT,
                                                    amount=Decimal('25.00'), due_date=date(2026, 1, 10))
        services.execute_transaction(pre_existing, executed_date=date(2026, 1, 10))

        statement = services.bootstrap_statement(self.card, Decimal('300.00'), date(2026, 3, 5), today=date(2026, 2, 1))

        pre_existing.refresh_from_db()
        self.assertEqual(pre_existing.statement_id, statement.pk)
        self.assertEqual(statement.statement_balance, Decimal('300.00'))  # the user's own figure, not re-summed

        later_statement = services.close_statement_if_due(self.card, today=date(2026, 3, 20))
        self.assertIsNotNone(later_statement)
        self.assertEqual(later_statement.statement_balance, Decimal('0.00'))  # nothing new, nothing re-claimed


class CloseStatementIfDueTests(TestCase):
    def setUp(self):
        self.card = Account.objects.create(
            name='Visa', account_type=Account.AccountType.CREDIT_CARD, current_balance=Decimal('0.00'),
            cut_day=15, payment_due_day=5, next_statement_cut_date=date(2026, 2, 15),
        )

    def make_purchase(self, amount, due_date, direction=Transaction.Direction.OUT):
        txn = Transaction.objects.create(account=self.card, direction=direction, amount=Decimal(amount),
                                          due_date=due_date)
        services.execute_transaction(txn, executed_date=due_date)
        return txn

    def test_not_due_yet_is_a_noop(self):
        self.make_purchase('30.00', date(2026, 1, 20))
        result = services.close_statement_if_due(self.card, today=date(2026, 2, 1))
        self.assertIsNone(result)
        self.assertEqual(CreditCardStatement.objects.count(), 0)

    def test_paying_a_statement_before_the_next_cut_does_not_corrupt_the_next_statement(self):
        # Regression: a statement's payment obligation materializes as an IN
        # transaction on the card (the Transfer's in_leg). If that payment is
        # executed before the NEXT cut runs, the next close's claimable query
        # (executed=True, statement__isnull=True, due_date<=cut_date) has no
        # reason to exclude it — it would get swept in as if it were a
        # genuine credit reducing that cycle's debt, corrupting the total.
        first = self.make_purchase('30.00', date(2026, 1, 20))
        first_statement = services.close_statement_if_due(self.card, today=date(2026, 2, 15))
        self.assertEqual(first_statement.statement_balance, Decimal('30.00'))

        funding = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        transfer = first_statement.payment_obligation
        services.assign_account(transfer.out_leg, funding)
        services.execute_transfer(transfer, executed_date=date(2026, 3, 1))  # paid well before the next cut

        # A genuine new purchase for the NEXT cycle.
        self.make_purchase('12.00', date(2026, 3, 10))

        second_statement = services.close_statement_if_due(self.card, today=date(2026, 3, 15))
        self.assertIsNotNone(second_statement)
        self.assertEqual(second_statement.statement_balance, Decimal('12.00'))
        transfer.in_leg.refresh_from_db()
        self.assertIsNone(transfer.in_leg.statement_id)  # the payment leg is never claimed by any statement

    def test_payment_due_day_cleared_after_cycling_started_pauses_rather_than_using_wrong_date(self):
        # Regression: next_statement_cut_date is already set (from setUp), so
        # the create-time/self-heal guards don't apply here — this covers a
        # user clearing payment_due_day on an ALREADY-cycling card.
        self.card.payment_due_day = None
        self.card.save()
        self.make_purchase('30.00', date(2026, 1, 20))

        result = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        self.assertIsNone(result)
        self.assertEqual(CreditCardStatement.objects.count(), 0)
        self.card.refresh_from_db()
        self.assertEqual(self.card.next_statement_cut_date, date(2026, 2, 15))  # unchanged, will retry

        # Reconfiguring it lets the paused cycle close normally afterward.
        self.card.payment_due_day = 5
        self.card.save()
        result = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        self.assertIsNotNone(result)
        self.assertEqual(result.statement_balance, Decimal('30.00'))

    def test_claims_only_transactions_in_window_even_when_checked_late(self):
        in_window_1 = self.make_purchase('30.00', date(2026, 1, 20))
        in_window_2 = self.make_purchase('20.00', date(2026, 2, 10))
        # This purchase happens AFTER the real cut_date but the check doesn't
        # run until several days later — it must NOT be absorbed into the
        # cycle that already closed.
        after_cut = self.make_purchase('15.00', date(2026, 2, 20))

        statement = services.close_statement_if_due(self.card, today=date(2026, 2, 25))

        self.assertIsNotNone(statement)
        self.assertEqual(statement.cut_date, date(2026, 2, 15))
        self.assertEqual(statement.due_date, date(2026, 3, 5))
        self.assertEqual(statement.statement_balance, Decimal('50.00'))

        in_window_1.refresh_from_db()
        in_window_2.refresh_from_db()
        after_cut.refresh_from_db()
        self.assertEqual(in_window_1.statement_id, statement.pk)
        self.assertEqual(in_window_2.statement_id, statement.pk)
        self.assertIsNone(after_cut.statement_id)

        self.card.refresh_from_db()
        self.assertEqual(self.card.next_statement_cut_date, date(2026, 3, 15))

    def test_is_idempotent(self):
        self.make_purchase('30.00', date(2026, 1, 20))
        first = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        second = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(CreditCardStatement.objects.count(), 1)

    def test_creates_unassigned_payment_obligation(self):
        self.make_purchase('30.00', date(2026, 1, 20))
        statement = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        self.assertIsNotNone(statement.payment_obligation)
        transfer = statement.payment_obligation
        self.assertIsNone(transfer.out_leg.account_id)
        self.assertEqual(transfer.in_leg.account_id, self.card.pk)
        self.assertEqual(transfer.out_leg.amount, Decimal('30.00'))
        self.assertFalse(statement.is_paid)

    def test_net_credit_cycle_bills_as_zero_with_no_transfer(self):
        self.make_purchase('30.00', date(2026, 1, 20))
        self.make_purchase('50.00', date(2026, 2, 1), direction=Transaction.Direction.IN)  # a refund exceeding the purchase
        statement = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        self.assertEqual(statement.statement_balance, Decimal('0.00'))
        self.assertIsNone(statement.payment_obligation)
        self.assertFalse(statement.is_paid)

    def test_paying_the_statement_updates_card_balance_and_is_paid(self):
        self.make_purchase('30.00', date(2026, 1, 20))
        statement = services.close_statement_if_due(self.card, today=date(2026, 2, 20))
        self.card.refresh_from_db()
        balance_before_payment = self.card.current_balance  # -30.00, from the purchase itself

        funding = Account.objects.create(name='Checking', current_balance=Decimal('1000.00'))
        transfer = statement.payment_obligation
        services.assign_account(transfer.out_leg, funding)
        services.execute_transfer(transfer)

        self.card.refresh_from_db()
        funding.refresh_from_db()
        statement.refresh_from_db()
        self.assertEqual(self.card.current_balance, balance_before_payment + Decimal('30.00'))
        self.assertEqual(funding.current_balance, Decimal('970.00'))
        self.assertTrue(statement.is_paid)


class CardConfiguredDirectlyRegressionTests(TestCase):
    """Regression coverage for a real bug: a credit card configured via the
    plain Account form (cut_day/payment_due_day set directly, e.g. a fresh
    $0 card that never goes through bootstrap_statement) never had
    next_statement_cut_date seeded anywhere, so cycling silently never
    activated even though the card looked fully configured."""

    def test_close_statements_for_all_cards_self_heals_missing_cut_date(self):
        card = Account.objects.create(
            name='Visa', account_type=Account.AccountType.CREDIT_CARD, current_balance=Decimal('0.00'),
            cut_day=15, payment_due_day=5,
        )
        self.assertIsNone(card.next_statement_cut_date)

        services.close_statements_if_due_for_all_cards(today=date(2026, 2, 1))

        card.refresh_from_db()
        # today (Feb 1) is before this month's cut_day (15) — nearest upcoming
        # cut is THIS month, not next.
        self.assertEqual(card.next_statement_cut_date, date(2026, 2, 15))

    def test_card_missing_payment_due_day_is_not_seeded(self):
        # Without payment_due_day, close_statement_if_due would have no valid
        # day to compute a due_date from — so cycling must not activate until
        # both fields are configured, rather than silently using a wrong date.
        card = Account.objects.create(
            name='Visa', account_type=Account.AccountType.CREDIT_CARD, current_balance=Decimal('0.00'),
            cut_day=15,
        )
        services.close_statements_if_due_for_all_cards(today=date(2026, 2, 1))
        card.refresh_from_db()
        self.assertIsNone(card.next_statement_cut_date)
