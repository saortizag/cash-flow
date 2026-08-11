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
  never touches the balance until you say it happened.
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
└── tests/                   93 tests — see Testing below

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
| Dashboard | `/` | Assets / card debt / net worth, overdue & upcoming, unassigned obligations |
| Accounts | `/accounts/` | Create/edit accounts; `/accounts/credit-card-bootstrap/` records existing card debt with no itemized history |
| Categories | `/categories/` | Tag transactions for later breakdown |
| Transactions | `/transactions/` | Create, edit, execute/un-execute, delete, assign an account |
| Transfers | `/transfers/` | Move money between two accounts |
| Recurring | `/recurring/` | Templates that auto-generate future transactions |
| Projection | `/projection/` | Pick a date, see the projected balance and what gets you there |
| Admin | `/admin/` | Full model access, routed through `services.py` for the models that need it |

---

## Testing

```bash
python manage.py test ledger
```

93 tests, focused on the highest-risk logic: balance-mutation atomicity and
concurrency safety, recurring-generation date math (including month-end
edge cases), credit-card statement claiming and cut-date seeding, transfer
all-or-nothing execution, and unassigned-payment projection accuracy. Run
this before trusting any change to `services.py`.

Optional: `python manage.py generate_recurring_occurrences` /
`--horizon-months N` runs the same recurring sweep as a management command,
for cron use instead of relying on page loads.
