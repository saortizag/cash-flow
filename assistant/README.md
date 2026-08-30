# Cash assistant

Record transactions into Cash by typing a short, informal message instead of filling out a
form:

```
> 121334 con cencosud en mercado de carulla hoy
Gasto registrado: $121,334 en Mercado con TC Cencosud (Carulla).
> 14000 a checho
¿en qué categoría lo pongo y con qué pagaste?
> restaurantes, efectivo
Gasto registrado: $14,000 en Restaurantes con Efectivo (checho).
```

If the message already has everything needed, it's recorded immediately. If something's
missing, the assistant asks for just that, remembers what it already knows, and records the
transaction once it has enough.

This is a standalone component: its own dependencies, its own `.env`, and it never imports
Django or touches the database directly — it only ever talks to the real Cash REST API (`api/`),
exactly like any other API client would (a script, `curl`, a future WhatsApp bot).

## Setup

1. Install [Ollama](https://ollama.com) and start it (`ollama serve`, or it may already run as a
   background service).
2. Pull a model: `ollama pull mistral` — a small (~4GB), fast model that works well once the
   prompt in `extraction.py` gives it a couple of examples to imitate (see "Why few-shot
   examples" below). A newer/bigger model (`llama3.1`, `qwen2.5`) will be more reliable on
   ambiguous input at the cost of speed/RAM — swap it via `OLLAMA_MODEL` in `.env`, no code
   changes needed.
3. `pip install -r requirements.txt` (a virtualenv is recommended, kept separate from the main
   Django project's own dependencies).
4. `cp .env.example .env` and fill in `CASH_USERNAME`/`CASH_PASSWORD` with real Cash login
   credentials, and `CASH_API_URL` (`http://127.0.0.1:8000` for a local `runserver`).
5. Make sure the Cash server is running (`python manage.py runserver` from the repo root).
6. `python cli.py`

Type `salir` (or `exit`/`quit`) to stop.

## How it's built

Four files, each with one job — this split is what makes it possible to test the logic without
a running LLM, and to swap the CLI for WhatsApp later without touching the other three:

| File | Job |
|---|---|
| `extraction.py` | Text → structured guess. The **only** file that talks to the LLM. |
| `agent.py` | Decides whether the guess is complete; if not, asks; if so, calls the API. All plain Python — no LLM calls. |
| `api_client.py` | HTTP client for the real Cash API (JWT auth, list accounts/categories, create a transaction). No LLM, no Django. |
| `cli.py` | The interface: reads a line, calls `agent.handle_message()`, prints the reply. |

```
cli.py  ──(text, session_id)──>  agent.py  ──(conversation)──>  extraction.py ──> Ollama
           <──(reply text)──         │
                                      └──(HTTP, JWT)──> api_client.py ──> Cash REST API
```

### Why the LLM never calls the API directly

A tempting alternative design is a LangChain "agent" that's handed a `create_transaction` tool
and decides for itself when to call it (the standard "tool-calling agent" pattern LangChain is
usually shown off with). This deliberately does **not** do that. The LLM's only job is text →
`TransactionExtraction` (see `extraction.py`) — a plain data-extraction call, not a decision. The
*decision* — "is this complete enough to record, and what should the API call look like" — is
ordinary `if` statements in `agent.py`.

Reasoning: a small local model is good at "what did this sentence say" and unreliable at "should
I take an action with real money attached." Keeping the action deterministic means a bug or a bad
day for the model can produce a wrong *guess* (fixable — it just asks again or is easy to spot in
a confirmation message) but never a wrong or duplicate *API call*.

### Why few-shot examples, not just instructions

The first version of the prompt in `extraction.py` just *told* the model "if you're not sure,
leave it null." Tested against the user's own `"14000 a checho"` example, `mistral:latest`
confidently invented an account anyway — small local models are often poorly *calibrated*: they
don't reliably know what they don't know, so an instruction to abstain is easy for them to ignore
under confidence. Showing the model one worked example of the correct "leave it null" behavior,
right in the system prompt, fixed this completely. This is a general lesson, not specific to this
project: for a small model, **show, don't just tell**.

### Why `method="json_mode"` in `extraction.py`

`with_structured_output()` normally asks Ollama for JSON via a schema object. The Ollama version
in this environment (`ollama --version`) predates that feature and only supports the older
`format="json"` (plain string) mode — passing the schema object fails with a Go type error on the
Ollama side. `method="json_mode"` switches to the older, compatible mode; the tradeoff is that the
expected keys have to be spelled out in the prompt text itself (already done in
`extraction.py`'s `_PROMPT_TEMPLATE`), since `json_mode` doesn't inject the schema automatically.
If you're running a newer Ollama, the default method works too and enforces the schema more
strictly — this is purely a compatibility fallback, not a design preference.

### Session state and the clarifying-question loop

A single message can be incomplete, so the agent needs to remember a half-finished transaction
between messages. `SessionStore` (in `agent.py`) is an in-memory dict keyed by `session_id` — for
the CLI, that's a fixed string, since one process = one conversation. On every message, the
**whole conversation so far** (not just the latest line) is re-sent to the model, which lets it
naturally connect a short answer like `"restaurantes, efectivo"` back to the two blanks it just
asked about — no separate answer-parsing code needed. A small merge step (`_merge` in `agent.py`)
then layers the new guess on top of the old one, field by field, as a safety net in case a later
turn's extraction accidentally nulls out something an earlier turn had already gotten right.

### Name resolution

The LLM is told the user's real, current account/category names (fetched live from the API) and
asked to pick one of those exact names — but a small model can still typo or paraphrase one
(`"cencosud"` instead of `"TC Cencosud"`). `_resolve_name()` matches case-insensitively first,
then falls back to a fuzzy match (`difflib.get_close_matches`) for near-misses. If nothing is
close enough, it's treated exactly like the model having said nothing at all: the field counts as
missing, and the agent asks again — this is also what happens if the model names an account that
doesn't exist for this user at all.

### What gets recorded

Every transaction created through this assistant is recorded as **already executed**
(`executed=True`, `executed_date=today`) — this is a "record what just happened" tool, matching
both of the design examples (a purchase that already happened), not a scheduler for future/planned
transactions. Recording a not-yet-due future transaction isn't something this conversational flow
supports; use the web UI for that.

### Swapping the CLI for WhatsApp later

`cli.py` is the only file that would be replaced. A WhatsApp adapter is the same shape — receive
`(sender_phone_number, text)` from a webhook, call `agent.handle_message(sender_phone_number,
text)`, send the reply back through the WhatsApp API — plus one change inside `agent.py`:
`SessionStore`'s in-memory dict would need to become something shared across requests (Redis, a
DB table), since a webhook process can restart or run as multiple workers between two messages
from the same person, unlike a single long-lived CLI process. `extraction.py` and `api_client.py`
would not change at all.

## Tests

```
python -m unittest tests.test_agent -v
```

Mocks the LLM boundary (patches `extract()` directly) so these run in well under a second, with
no Ollama server needed — they check the clarification loop, the merge safety net, name
resolution, and the exact payload sent to the Cash API. Whether a real model extracts well from
real messages is a separate question, validated manually against a live Ollama instance rather
than as an automated test (slow and non-deterministic by nature).
