"""Turns a short, informal message ("121334 con cencosud en mercado de carulla hoy") into a
structured, partially-filled TransactionExtraction — the only piece of this app that talks to
the LLM. Everything downstream (agent.py) is plain, deterministic Python.

Kept deliberately narrow: this module's only job is text -> structured guess. It never decides
whether the guess is complete, never asks a question, never calls the Cash API. That separation
is what makes the rest of the system testable without a live LLM.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TransactionExtraction(BaseModel):
    """Everything nullable on purpose — "I don't know" must be a representable answer, not
    something the model has to fake by guessing. agent.py decides what "missing" means."""

    amount: Optional[float] = Field(None, description="The monetary amount mentioned, always positive")
    direction: Optional[Literal['IN', 'OUT']] = Field(
        None, description='OUT if money was spent/paid, IN if money was received')
    account_name: Optional[str] = Field(
        None, description='Which ONE of the known accounts this was paid with, if named or clearly implied')
    category_name: Optional[str] = Field(
        None, description='Which ONE of the known categories this belongs to, if named or clearly implied')
    description: Optional[str] = Field(None, description='A short 2-6 word description of what this was for')


# Few-shot examples matter more than instructions for a small local model's calibration — the
# first version of this prompt (instructions only) had mistral:latest confidently invent an
# account name for a message that named none. Showing it one worked "leave it null" example and
# one worked "fill everything in" example fixed that completely — verified live against real
# examples before this was written this way.
_PROMPT_TEMPLATE = """You extract structured personal-finance transaction data from short, informal \
Spanish/Spanglish messages a user types to record a purchase or payment.

Known accounts (payment methods): {accounts}
Known categories: {categories}

Only set account_name/category_name to one of the exact names above if you're confident. If the \
message doesn't clearly indicate one, leave it null — do not guess.

Respond with ONLY a JSON object with exactly these keys: amount (number or null), direction \
("IN" or "OUT" or null), account_name (string or null), category_name (string or null), \
description (string or null). No prose, no markdown fences, just the JSON object.

Example — the message names no payment method or category at all, so BOTH must be null even
though it would be tempting to guess a common one:
Message: "14000 a checho"
{{"amount": 14000, "direction": "OUT", "account_name": null, "category_name": null, "description": "Checho"}}

Example — the message clearly names both:
Message: "50000 con nu en transporte uber"
{{"amount": 50000, "direction": "OUT", "account_name": "Nu", "category_name": "Transporte", "description": "Uber"}}"""


def build_system_prompt(account_names: list[str], category_names: list[str]) -> str:
    """account_names/category_names should be the user's REAL, current accounts/categories
    (fetched from the API — see api_client.py) so the model grounds its answer in things that
    actually exist, rather than whatever it's seen in training data."""
    return _PROMPT_TEMPLATE.format(accounts=', '.join(account_names), categories=', '.join(category_names))


def extract(llm, system_prompt: str, conversation: list[tuple[str, str]]) -> TransactionExtraction:
    """conversation is the full back-and-forth so far as (role, text) pairs, e.g.
    [("human", "14000 a checho"), ("ai", "..."), ("human", "restaurantes, efectivo")] — passing
    the WHOLE conversation each time (not just the latest reply) lets the model naturally
    connect a short answer like "restaurantes, efectivo" back to the two blanks it just asked
    about, with no separate answer-parsing logic needed.

    method="json_mode" rather than the library default: this Ollama server predates the newer
    schema-based structured output (see assistant/README.md) — json_mode's plain format="json"
    has been supported since much earlier Ollama versions. A newer Ollama + a tool-calling model
    (llama3.1, qwen2.5) can use the default method for stricter enforcement instead."""
    structured_llm = llm.with_structured_output(TransactionExtraction, method='json_mode')
    return structured_llm.invoke([('system', system_prompt), *conversation])
