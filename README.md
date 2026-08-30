# Cash

A personal cash-flow tracker: accounts, transactions, recurring bills,
transfers, and credit card billing cycles, built to answer one question —
**how much money will I actually have on a given future date?**

Django + PostgreSQL, server-rendered templates styled with Bootstrap
(`django-bootstrap5`). Single user, single currency, local-first.

---

## Features

- **Accounts** — cash, bank checking/savings, credit cards, or other. Each
  has a directly-settable `current_balance` (for an opening balance or
  reconciling against a real statement).
- **Transactions** — an amount, direction (in/out), a due date, and an
  `executed` flag. A transaction only affects its account's balance once
  executed — a due date in the future (or even the past, if unexecuted)
  never touches the balance until you say it happened. Logging something
  already executed doesn't require a separate due date — it defaults to
  the execution date. The list defaults to most-recently-active first
  with future-dated pending items hidden (recurring bills alone generate
  a 12-month horizon that would otherwise swamp it) — sortable by any
  column, paginated at 50 per page, with a one-click way to reveal the
  hidden future items.
- **Recurring transactions** — a template (weekly/monthly/quarterly/yearly,
  every N periods) that materializes real transaction rows on a rolling
  12-month horizon, so rent, salary, and subscriptions show up automatically
  without re-entering them every cycle.
- **Transfers** — move money between two accounts as one action; schedulable
  like a regular transaction (a future due date, executed later) rather than
  always-instant.
- **Unassigned future payments** — record "I'll owe $X on date D" before
  deciding which account will fund it. It still counts toward the combined
  cash-flow projection; you assign (and later execute) it once you decide.
- **Credit cards** — purchases are ordinary transactions that increase the
  card's debt immediately; a monthly cut-off date (*fecha de corte*) and
  payment-due day drive automatic billing-cycle closes, which snapshot
  what's owed and generate an (initially unassigned) payment transfer due on
  the right date. A "record what I already owe" flow bootstraps a card that
  already carries a balance with no itemized history behind it yet.
- **Cash-flow projection** — pick a date, see current balance plus every
  still-pending transaction due by then, per account and combined, with a
  running balance at each step.
- **Expense chart** — a bar chart of executed spending on the dashboard,
  toggled between day/week/month resolution with a pill-button selector
  (CSS-only, no JavaScript).
- **Support-file attachments** — an optional receipt, invoice, or statement
  (PDF, image, or office doc, max 10MB) on any transaction or transfer.
  Stored locally and only ever served back through an authenticated
  download — never a public URL.
- **REST API** (`/api/v1/`) — everything above, over JSON, for other
  consumers (scripts, automations, a future mobile client). JWT-authenticated,
  routes through the same `services.py` the templates use. See
  [REST API](#rest-api) below.
- **A git-based "mind"** (`ONEMIND.md` / `AGENTS.md`) — this repo also
  carries a persistent memory for AI coding agents working on it, stored as
  git objects on a hidden ref (`refs/mind/main`) that never shows up in
  `git status`/`git log`/`git branch`. See `ONEMIND.md` for the spec.

---

## Tech stack

| | |
|---|---|
| Backend | Django 6.1 |
| Database | PostgreSQL (local) |
| Frontend | Django templates + `django-bootstrap5` (no SPA, no JS build step) |
| REST API | Django REST Framework, `djangorestframework-simplejwt` (JWT auth), `django-filter`, `drf-spectacular` (OpenAPI docs) |
| Driver | `psycopg[binary]` (psycopg 3) |
| Date math | `python-dateutil` (`relativedelta`, for month-end-safe recurring/cut-date math) |
| Config | `python-dotenv` (`.env`, git-ignored) |
| Python | 3.14, via the `web` conda environment |

---

## Project structure

```
cash/                       # Django project config
├── settings.py             # dotenv-based config; DEBUG/ALLOWED_HOSTS/TIME_ZONE from .env
├── urls.py                 # mounts /login/, /logout/, and ledger.urls at /
└── ...

ledger/                     # the one app — everything lives here
├── models.py                Account, Category, Transaction, RecurringTransaction,
│                             Transfer, CreditCardStatement
├── services.py              the ONLY sanctioned way to mutate a balance or
│                             materialize recurring/statement rows — see below
├── forms.py, views.py, admin.py, urls.py
├── templatetags/
│   └── ledger_extras.py     `money` filter: formats amounts as $1,234,567.89
├── management/commands/
│   └── generate_recurring_occurrences.py   manual/cron-friendly recurring sweep
├── templates/ledger/        one template per page (list/form/confirm/detail)
└── tests/                   113 tests — see Testing below

api/                        the REST API — no models of its own, no templates
├── serializers.py           one per ledger model + action-only ones (execute, assign-account, ...)
├── views.py                 ViewSets + reporting views, all calling ledger.services
├── filters.py                django-filter FilterSet for the transaction list endpoint
├── exceptions.py             translates services.py's ValidationError/ProtectedError into 400s
├── urls.py                   /api/token/, /api/schema/, /api/docs/, /api/v1/...
└── tests/                    69 tests — see Testing below

media/                       uploaded support files (git-ignored — see Features)
Cash.postman_collection.json  importable Postman collection covering every endpoint (see REST API)
ONEMIND.md, AGENTS.md         the git-based agent memory system (see Features)
manage.py, requirements.txt, .env.example, .gitignore
```

---

## Setup

### Prerequisites

- PostgreSQL running locally.
- A Python 3.14 environment with the dependencies in `requirements.txt`
  installed (this project was built and is run against the `web` conda
  environment: `/home/santiago/miniconda3/envs/web/bin/python`).

### 1. Install dependencies

```bash
/home/santiago/miniconda3/envs/web/bin/pip install -r requirements.txt
```

### 2. Create the database and role

Postgres 15+ revokes `CREATE` on the `public` schema from `PUBLIC` by
default, so the app's role needs to own that schema in its own database —
not just have privileges granted on it.

```bash
psql -U <superuser> -d postgres -c "CREATE DATABASE cash_db;"
psql -U <superuser> -d postgres -c "CREATE ROLE cash_user WITH LOGIN PASSWORD '<a strong password>';"
psql -U <superuser> -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE cash_db TO cash_user;"
psql -U <superuser> -d cash_db  -c "ALTER SCHEMA public OWNER TO cash_user;"
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

```
SECRET_KEY=<python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

DB_NAME=cash_db
DB_USER=cash_user
DB_PASSWORD=<the password from step 2>
DB_HOST=localhost
DB_PORT=5432

TIME_ZONE=America/Bogota
```

`TIME_ZONE` matters more than it looks: it's what Django uses to decide
"today" for due dates and credit-card cut-off checks. Set it to wherever
you actually are — running it as `UTC` while living somewhere else means
"today" can flip over hours before your own midnight (this bit us once;
see the mind log).

### 4. Migrate and create your login

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 5. Run it

```bash
python manage.py runserver
```

Visit `http://127.0.0.1:8000/`, log in, and start with **Accounts → New
account**.

---

## How it works

### The balance-integrity rule

`ledger/services.py` is the *only* code path allowed to change
`Account.current_balance` from a transaction, or to flip a transaction's
`executed` flag. Every function that checks `executed` re-fetches the row
under `select_for_update()` rather than trusting whatever the caller already
had in memory — two concurrent submits for the same transaction (a
double-click, two browser tabs) must not both apply the balance change. Lock
ordering is consistent across every function that touches more than one row,
to avoid deadlocking against itself. The Django admin routes through the
same service functions rather than editing rows directly, for the same
reason.

### An executed transaction's due date defaults to its execution date

`due_date` is required unless you're logging something already executed (`services.
create_transaction`/`create_transfer`, both `due_date=None` by default) — in that case an
omitted due date defaults to the resolved execution date (today, if you don't give one either),
since retroactively logging something that already happened shouldn't also require separately
typing the same date into a due-date field that no longer plans anything. An explicit due date
is still respected if you give one (e.g. "due the 1st, actually paid the 3rd").

### Credit cards use a negative-balance convention

A credit card's `current_balance` is negative (`-450.00` = you owe 450). A
purchase is just an ordinary `OUT` transaction; a payment is just an
ordinary `IN` transaction — the balance math needs zero special-casing for
card accounts. `cut_day` and `payment_due_day` (both 1–31) drive automatic
billing: `close_statement_if_due` runs opportunistically (checked whenever
the dashboard or projection page loads) and, once a cut date has passed,
snapshots everything executed-and-unclaimed up to that date into a new
`CreditCardStatement`, then generates its payment as an **unassigned**
`Transfer` to the card — you assign a funding account and execute it
whenever you're ready to pay.

### Transfers are two linked transactions, not a separate ledger

A `Transfer` is a thin wrapper around two `Transaction` rows (an `OUT` leg
on the source, an `IN` leg on the destination) rather than its own
balance-affecting model. That means transfers automatically show up
everywhere a transaction would — the transaction list, the dashboard, the
projection's running-balance table — with no separate query logic to keep
in sync. A transfer's individual legs can't be edited/executed/deleted
directly through the plain transaction pages; only through the transfer
itself, so both sides always move together.

### Unassigned payments

`Transaction.account` (and a transfer's `out_leg.account`) can be `None` — a
real future obligation with no funding account decided yet. It shows up on
the dashboard's "Needs an account assigned" section and counts toward the
projection's *combined* total (never a per-account one, since it isn't
assigned to any account). This is also how a credit card statement's
payment starts out: generated with no source account until you pick one.

### Support files never touch a public URL

`Transaction.attachment` (and `Transfer.attachment`, a property that reads from the transfer's
`out_leg` — one file per transfer, not two) is a plain local `FileField`, restricted to a
conservative extension allow-list plus a 10MB size cap. `MEDIA_ROOT` is configured but
deliberately **not** wired into `urls.py` as a public static route — these can be financial
documents, so the only way to read one back is `transaction_attachment_download` /
`transfer_attachment_download` (and their `api/` equivalents), both behind the same login gate
as everything else. Uploading, replacing, or clearing one is allowed regardless of a
transaction/transfer's executed state (it's not a financial field), but is deliberately routed
through its own dedicated view/action rather than the regular edit forms, to keep Django's
`FileField` "no change" vs "clear" vs "replace" tri-state logic in one place. Replacing or
deleting an attachment also deletes the old file from disk (not just the database reference) —
via `transaction.on_commit`, so a rolled-back request never deletes a file a surviving row still
points at.

### The expense chart is bucketed in Python, not the database

`services.expense_totals_by_period(resolution)` sums executed, `OUT`-direction transactions
into day/week/month buckets — a fixed trailing window per resolution (30 days / 12 weeks / 12
months) ending today, zero-filled so a quiet day still shows as an empty bar rather than
shifting the spacing. Two choices worth knowing:
- Bucketed by **`executed_date`** (when the money actually left), not `due_date` — this is a
  historical spending chart, not a plan, and the two dates can differ (a bill's due date vs. the
  day you actually paid it).
- **Transfers are excluded.** A transfer's `OUT` leg moves money between your own accounts, it
  isn't spending — counting it would also double-count a credit card payment on top of the
  purchases it settles (already counted when they happened, as ordinary `OUT` transactions on
  the card).

Bucket boundaries are plain Python date math (a week starts Monday, ISO-style), not
`TruncWeek`/`TruncMonth` — this keeps the boundary logic in one place we control and can test
precisely, rather than depending on a database function's behavior. The day/week/month toggle on
the dashboard is pure CSS (radio inputs + `:checked ~` sibling selectors): all three datasets
render server-side on every load, and only one is visible at a time — no fetch, no JavaScript.

### The transaction list defaults to recent activity, not the full plan

With no explicit `?sort=` column chosen, the list orders by `executed_date` for executed rows
(when the money actually left) falling back to `due_date` for the pending rows still shown
(overdue or due today) — most recent first, via `Coalesce('executed_date', 'due_date')` — rather
than any single literal column. Clicking a column header (due date, account, category,
description, amount, status) switches to sorting by that literal field instead, ascending, and
toggles direction on a second click; "Reset" (or just not touching `?sort=`) goes back to the
smart default.

Future-dated *pending* transactions are excluded from this view by default — recurring
templates alone generate a 12-month horizon of these, which would otherwise dominate what's
meant to be a record of what actually happened. An executed transaction with a future `due_date`
is not affected (it already happened, regardless of what its due date says); a banner with a
"Show future" link reveals everything (`?future=show`) when you do want to see the plan ahead.

### Sorting by amount sorts by magnitude, not signed value

`Transaction.amount` is always stored positive (a DB constraint enforces `amount > 0`); the
`+`/`-` shown in templates is a display-only convention driven by `direction`, not part of the
stored value. Sorting the transaction list by Amount therefore orders by size regardless of
in/out — e.g. a `$3,000` expense sorts before a `$4,390` one, even though the expense displays
with a `-` and would look "smaller." Every other sortable column (due date, account, category,
description, status) sorts on its literal field value; the sortable-column set itself is an
explicit whitelist in `ledger.views.TRANSACTION_SORT_FIELDS`, not the raw query string — passing
an arbitrary field name to `order_by()` would otherwise let a request probe unintended columns.

### Recurring generation and statement cycling are both self-healing

Both `ensure_recurring_horizon` and `close_statement_if_due` are checked
opportunistically from page loads rather than a cron job, and both are
written to be idempotent regardless of *when* they actually run — recurring
generation always walks the full schedule from its anchor date rather than
resuming from a cached position (so out-of-order execution or an edited
schedule can't desync it), and statement closing claims transactions
explicitly (a `statement` FK) rather than inferring membership from a live
balance snapshot (so running late never absorbs purchases from the next
cycle into the one that already closed).

---

## Usage overview

| Page | URL | What it's for |
|---|---|---|
| Dashboard | `/` | Assets / card debt / net worth, expense chart (day/week/month), overdue & upcoming, unassigned obligations |
| Accounts | `/accounts/` | Create/edit accounts; `/accounts/credit-card-bootstrap/` records existing card debt with no itemized history |
| Categories | `/categories/` | Tag transactions for later breakdown |
| Transactions | `/transactions/` | Create, edit, execute/un-execute, delete, assign an account, attach a support file; recent-first by default (future pending hidden, toggle to reveal), sortable by column, 50 per page |
| Transfers | `/transfers/` | Move money between two accounts, attach a support file |
| Recurring | `/recurring/` | Templates that auto-generate future transactions |
| Projection | `/projection/` | Pick a date, see the projected balance and what gets you there |
| Admin | `/admin/` | Full model access, routed through `services.py` for the models that need it |

---

## REST API

`api/` exposes the same functionality as JSON for other consumers — everything routes through
`ledger/services.py`, the same functions the templates above call, so the two surfaces can never
disagree on a balance.

### Authentication

JWT (`djangorestframework-simplejwt`). Get a token pair with your normal login credentials:

```bash
curl -X POST http://127.0.0.1:8000/api/token/ -d "username=<you>&password=<your password>"
# {"access": "...", "refresh": "..."}
```

Send the access token on every request:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/ -H "Authorization: Bearer <access>"
```

Access tokens last 1 hour; refresh at `/api/token/refresh/` with the refresh token (14 days).

### Endpoints

Interactive docs (OpenAPI/Swagger) are always up to date at `/api/docs/` — the table below is a
quick reference. A ready-to-import **[Postman collection](Cash.postman_collection.json)** covers
every endpoint below with working example bodies and a self-cleaning request order (run "Auth >
Obtain Token" first, then anything else — see the collection's own description for details).

Every list endpoint is paginated at 50 per page (`REST_FRAMEWORK.PAGE_SIZE`) — the response shape
is `{"count": N, "next": <url or null>, "previous": <url or null>, "results": [...]}`, not a bare
array. Request a page with `?page=N`; a page past the last one returns `404`. `/transactions/`
defaults to `due_date` ascending (earliest first), matching the web UI's own default.

| Resource | Path | Notes |
|---|---|---|
| Accounts | `/api/v1/accounts/` | CRUD; `POST .../{id}/bootstrap-statement/` records existing card debt |
| Categories | `/api/v1/categories/` | CRUD |
| Transactions | `/api/v1/transactions/` | CRUD (filter by `account` — incl. `unassigned` — `category`, `executed`, `direction`); `.../{id}/execute/`, `.../{id}/unexecute/`, `.../{id}/assign-account/`; `.../{id}/attachment/` (GET downloads, POST sets/replaces, DELETE clears) |
| Recurring transactions | `/api/v1/recurring-transactions/` | CRUD; `.../{id}/deactivate/` |
| Transfers | `/api/v1/transfers/` | CRUD (update is amount/description/due_date only — accounts aren't reassignable after creation); `.../{id}/execute/`, `.../{id}/unexecute/`; `.../{id}/attachment/` (same GET/POST/DELETE shape as transactions) |
| Credit card statements | `/api/v1/credit-card-statements/` | Read-only |
| Summary | `/api/v1/summary/` | Assets / card debt / net worth / overdue / upcoming / unassigned — the dashboard's numbers |
| Projection | `/api/v1/projection/summary/`, `/api/v1/projection/detail/` | `?target_date=&account=`; detail adds the per-transaction running-balance rows |

Same business rules as the UI apply everywhere: `due_date` is optional on create when
`executed=true` (defaults to `executed_date`, or today); an executed transaction can't have its
account/direction/amount changed (only category/description/due date — un-execute first);
a transfer leg can't be edited/executed/deleted through the plain transaction endpoints (manage it
via `/transfers/` instead — `assign-account` is the one exception, since that's how an
auto-generated credit-card payment obligation gets funded); `ValidationError`/`ProtectedError`
raised by `services.py` come back as clean `400` responses instead of crashing.

---

## AI assistant (optional)

`assistant/` records transactions from a short, informal message ("14000 a checho") instead of a
form, using a local LLM (via LangChain + Ollama — no cloud API, no cost) to extract the
structured fields and this same REST API to record them. It's a standalone component with its
own dependencies — see **[assistant/README.md](assistant/README.md)** for setup and an
explanation of how it's built.

---

## Testing

```bash
python manage.py test          # both apps
python manage.py test ledger   # 153 tests — template views, services.py, forms
python manage.py test api      # 78 tests — REST endpoints, auth, serializer validation
```

231 tests total, focused on the highest-risk logic: balance-mutation atomicity and
concurrency safety, recurring-generation date math (including month-end
edge cases), credit-card statement claiming and cut-date seeding, transfer
all-or-nothing execution, unassigned-payment projection accuracy, attachment
validation/storage-cleanup/auth-gating, expense-chart bucketing (zero-fill,
transfer/income exclusion, executed_date vs. due_date), transaction-list sorting/
pagination/future-visibility, due-date auto-fill on already-executed records, and —
on the API side — that the same business rules and numbers hold through JSON (auth,
field-locking on executed transactions, transfer-leg guards, pagination envelopes,
and summary/projection figures matching `services.py` called directly). Run this
before trusting any change to `services.py`.

Optional: `python manage.py generate_recurring_occurrences` /
`--horizon-months N` runs the same recurring sweep as a management command,
for cron use instead of relying on page loads.
