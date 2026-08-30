"""Tests the orchestration logic in agent.py with the LLM boundary mocked out (extract() is
patched directly, so these run instantly with no Ollama server needed). What's NOT tested here:
whether a real local model actually extracts well from real messages — that was validated
manually against a live model before this was built (see assistant/README.md) and isn't
something worth re-checking on every test run, since it's slow and non-deterministic.
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import TransactionAgent, _build_question, _merge, _resolve_name  # noqa: E402
from api_client import CashApiError  # noqa: E402
from extraction import TransactionExtraction  # noqa: E402

ACCOUNTS = [
    {'id': 1, 'name': 'Nu'},
    {'id': 2, 'name': 'TC Cencosud'},
]
CATEGORIES = [
    {'id': 10, 'name': 'Mercado'},
    {'id': 11, 'name': 'Restaurantes'},
]


class FakeApi:
    """Stands in for CashApiClient: agent.py only ever calls these three methods, so a stub
    matching that shape is simpler and faster than mocking HTTP requests."""

    def __init__(self):
        self.created = []

    def list_accounts(self):
        return ACCOUNTS

    def list_categories(self):
        return CATEGORIES

    def create_transaction(self, **kwargs):
        self.created.append(kwargs)
        return {'id': 999, **kwargs}


class FailingApi(FakeApi):
    def create_transaction(self, **kwargs):
        raise CashApiError('POST /api/v1/transactions/ -> 400: {"amount": ["..."]}')


def _extraction(**overrides):
    fields = dict(amount=None, direction=None, account_name=None, category_name=None, description=None)
    fields.update(overrides)
    return TransactionExtraction(**fields)


class MergeTests(unittest.TestCase):
    def test_new_value_wins(self):
        old = _extraction(amount=100)
        new = _extraction(amount=200)
        self.assertEqual(_merge(old, new).amount, 200)

    def test_null_does_not_erase_known_value(self):
        old = _extraction(amount=100, direction='OUT')
        new = _extraction(amount=None, category_name='Mercado')
        merged = _merge(old, new)
        self.assertEqual(merged.amount, 100)
        self.assertEqual(merged.direction, 'OUT')
        self.assertEqual(merged.category_name, 'Mercado')


class ResolveNameTests(unittest.TestCase):
    def test_exact_case_insensitive_match(self):
        self.assertEqual(_resolve_name('nu', ACCOUNTS)['id'], 1)

    def test_fuzzy_match_for_near_miss(self):
        self.assertEqual(_resolve_name('Cencosud', ACCOUNTS)['id'], 2)

    def test_no_match_returns_none(self):
        self.assertIsNone(_resolve_name('Efectivo', ACCOUNTS))

    def test_none_input_returns_none(self):
        self.assertIsNone(_resolve_name(None, ACCOUNTS))


class BuildQuestionTests(unittest.TestCase):
    def test_matches_users_own_example_phrasing(self):
        question = _build_question(['category', 'account'])
        self.assertEqual(question, '¿en qué categoría lo pongo y con qué pagaste?')


class HandleMessageTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.agent = TransactionAgent(llm=None, api=self.api)

    @patch('agent.extract')
    def test_complete_message_records_directly(self, mock_extract):
        mock_extract.return_value = _extraction(
            amount=121334, direction='OUT', account_name='TC Cencosud',
            category_name='Mercado', description='Carulla',
        )
        reply = self.agent.handle_message('s1', '121334 con cencosud en mercado de carulla hoy')

        self.assertIn('121,334', reply)
        self.assertEqual(len(self.api.created), 1)
        self.assertEqual(self.api.created[0], {
            'account_id': 2, 'category_id': 10, 'direction': 'OUT', 'amount': 121334,
            'description': 'Carulla', 'executed': True, 'executed_date': date.today(),
        })
        self.assertIsNone(self.agent.sessions.get('s1'))  # cleared after a successful recording

    @patch('agent.extract')
    def test_incomplete_message_asks_and_remembers_state(self, mock_extract):
        mock_extract.return_value = _extraction(amount=14000, direction='OUT', description='Checho')
        reply = self.agent.handle_message('s2', '14000 a checho')

        self.assertEqual(reply, '¿en qué categoría lo pongo y con qué pagaste?')
        self.assertEqual(self.api.created, [])
        pending = self.agent.sessions.get('s2')
        self.assertIsNotNone(pending)
        self.assertEqual(pending.extraction.amount, 14000)

    @patch('agent.extract')
    def test_answer_to_clarifying_question_completes_transaction(self, mock_extract):
        # Turn 1 extracts amount/direction/description; turn 2 (the user's answer to the
        # clarifying question) extracts only the two fields it was asked about — exercising the
        # merge, not just a single-shot extraction.
        mock_extract.side_effect = [
            _extraction(amount=14000, direction='OUT', description='Checho'),
            _extraction(account_name='Nu', category_name='Restaurantes'),
        ]
        self.agent.handle_message('s3', '14000 a checho')
        reply = self.agent.handle_message('s3', 'restaurantes, nu')

        self.assertIn('Restaurantes', reply)
        self.assertEqual(len(self.api.created), 1)
        created = self.api.created[0]
        self.assertEqual(created['amount'], 14000)
        self.assertEqual(created['account_id'], 1)
        self.assertEqual(created['category_id'], 11)
        self.assertIsNone(self.agent.sessions.get('s3'))

    @patch('agent.extract')
    def test_unresolvable_account_name_is_treated_as_missing(self, mock_extract):
        # The model named an account that isn't in this user's real, live account list — must be
        # treated exactly like "didn't say," not passed through to the API.
        mock_extract.return_value = _extraction(
            amount=14000, direction='OUT', account_name='Efectivo', category_name='Restaurantes',
        )
        reply = self.agent.handle_message('s4', '14000 en efectivo, restaurantes')

        self.assertEqual(reply, '¿con qué pagaste?')
        self.assertEqual(self.api.created, [])

    @patch('agent.extract')
    def test_api_failure_keeps_session_alive_instead_of_losing_progress(self, mock_extract):
        mock_extract.return_value = _extraction(
            amount=14000, direction='OUT', account_name='Nu', category_name='Restaurantes',
        )
        agent = TransactionAgent(llm=None, api=FailingApi())
        reply = agent.handle_message('s5', '14000 en efectivo, restaurantes')

        self.assertIn('No pude registrar', reply)
        self.assertIsNotNone(agent.sessions.get('s5'))  # not cleared — don't make the user retype it all


if __name__ == '__main__':
    unittest.main()
