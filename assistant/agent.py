"""The orchestrator. This is where the "does the agent have enough info yet?" decision lives —
deliberately as plain Python if-statements, not as something the LLM decides for itself. See
extraction.py's module docstring for why that split exists.

Flow per incoming message:
  1. Append it to this session's conversation.
  2. Ask the LLM to (re-)extract a TransactionExtraction from the whole conversation so far.
  3. Merge that with whatever was already known (a safety net — see _merge).
  4. Resolve account_name/category_name against the user's REAL accounts/categories.
  5. Still missing something required? Save state, ask a clarifying question, stop.
  6. Otherwise: call the real Cash API, clear the session, confirm.
"""
import difflib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from api_client import CashApiClient, CashApiError
from extraction import TransactionExtraction, build_system_prompt, extract

# amount/direction/category/account, in the order the clarifying question lists them — chosen to
# match the user's own example phrasing when both category and account are missing at once
# ("en que categoría lo pongo y con qué pagaste?").
_REQUIRED_FIELDS = ['amount', 'direction', 'category', 'account']

_QUESTION_FRAGMENTS = {
    'amount': 'cuánto fue',
    'direction': 'fue un ingreso o un gasto',
    'category': 'en qué categoría lo pongo',
    'account': 'con qué pagaste',
}


@dataclass
class PendingTransaction:
    conversation: list = field(default_factory=list)  # list[tuple[str, str]] of (role, text)
    extraction: TransactionExtraction = field(default_factory=TransactionExtraction)


class SessionStore:
    """In-memory, single-process — fine for a CLI talking to itself, since the CLI process IS
    the session. A WhatsApp webhook runs as a stateless server that could restart or scale to
    multiple workers between messages, so it would need this swapped for something shared
    (Redis, a DB table) keyed by phone number instead of a CLI's fixed session id. TransactionAgent
    only ever calls get/set/clear on this, so that swap is a one-class change — nothing in
    agent.py's own logic has to move.
    """

    def __init__(self):
        self._store: dict[str, PendingTransaction] = {}

    def get(self, session_id: str) -> Optional[PendingTransaction]:
        return self._store.get(session_id)

    def set(self, session_id: str, pending: PendingTransaction) -> None:
        self._store[session_id] = pending

    def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)


def _merge(old: TransactionExtraction, new: TransactionExtraction) -> TransactionExtraction:
    """A fresh extraction runs against the WHOLE conversation every turn (see extract()'s
    docstring), so in the common case it re-derives everything correctly on its own. This merge
    exists only as a safety net for the uncommon case where a later turn's extraction nulls out
    a field an earlier turn had already gotten right — new values win, but a null never erases
    something already known."""
    merged = old.model_dump()
    for key, value in new.model_dump().items():
        if value is not None:
            merged[key] = value
    return TransactionExtraction(**merged)


def _resolve_name(name: Optional[str], options: list[dict]) -> Optional[dict]:
    """options are the real account/category dicts from the live API (each has 'id' and 'name').
    Case-insensitive exact match first; difflib fuzzy match as a fallback for near-misses the LLM
    might introduce (e.g. "cencosud" vs "TC Cencosud"). Returns None — not an exception — when
    nothing is close enough; the caller treats that exactly like the field never having been
    extracted at all, and asks again."""
    if not name:
        return None
    by_lower_name = {opt['name'].lower(): opt for opt in options}
    if name.lower() in by_lower_name:
        return by_lower_name[name.lower()]
    close = difflib.get_close_matches(name.lower(), by_lower_name.keys(), n=1, cutoff=0.6)
    return by_lower_name[close[0]] if close else None


def _build_question(missing: list[str]) -> str:
    fragments = [_QUESTION_FRAGMENTS[field_name] for field_name in missing]
    return '¿' + ' y '.join(fragments) + '?'


def _build_confirmation(extraction: TransactionExtraction, account: dict, category: dict) -> str:
    kind = 'Gasto' if extraction.direction == 'OUT' else 'Ingreso'
    amount = f'{extraction.amount:,.0f}'
    line = f'{kind} registrado: ${amount} en {category["name"]} con {account["name"]}'
    return f'{line} ({extraction.description}).' if extraction.description else f'{line}.'


class TransactionAgent:
    def __init__(self, llm, api: CashApiClient, sessions: Optional[SessionStore] = None):
        self.llm = llm
        self.api = api
        self.sessions = sessions or SessionStore()

    def handle_message(self, session_id: str, text: str) -> str:
        pending = self.sessions.get(session_id)
        conversation = list(pending.conversation) if pending else []
        conversation.append(('human', text))

        # Fetched fresh every turn rather than cached: a personal finance tool has few enough
        # accounts/categories that this HTTP round-trip is negligible, and it means a category
        # added mid-conversation is usable immediately, with no cache-invalidation to think about.
        accounts = self.api.list_accounts()
        categories = self.api.list_categories()
        system_prompt = build_system_prompt(
            [account['name'] for account in accounts], [category['name'] for category in categories])

        new_extraction = extract(self.llm, system_prompt, conversation)
        extraction = _merge(pending.extraction, new_extraction) if pending else new_extraction

        resolved_account = _resolve_name(extraction.account_name, accounts)
        resolved_category = _resolve_name(extraction.category_name, categories)

        missing = []
        if extraction.amount is None:
            missing.append('amount')
        if extraction.direction is None:
            missing.append('direction')
        if resolved_category is None:
            missing.append('category')
        if resolved_account is None:
            missing.append('account')

        if missing:
            question = _build_question(missing)
            conversation.append(('ai', question))
            self.sessions.set(session_id, PendingTransaction(conversation=conversation, extraction=extraction))
            return question

        try:
            self.api.create_transaction(
                account_id=resolved_account['id'],
                category_id=resolved_category['id'],
                direction=extraction.direction,
                amount=extraction.amount,
                description=extraction.description or '',
                executed=True,
                executed_date=date.today(),
            )
        except CashApiError as exc:
            # Keep the session alive on failure — the user shouldn't have to retype everything
            # because the server rejected something (e.g. a validation rule this assistant
            # doesn't know about). They can just try again once whatever's wrong is fixed.
            self.sessions.set(session_id, PendingTransaction(conversation=conversation, extraction=extraction))
            return f'No pude registrar la transacción: {exc}'

        self.sessions.clear(session_id)
        return _build_confirmation(extraction, resolved_account, resolved_category)
