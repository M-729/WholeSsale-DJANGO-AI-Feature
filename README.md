# Wholesale Accounting & Business Management System

A full-stack Django application for a wholesale business: purchasing, inventory,
sales, payments, and an accrual-basis double-entry general ledger.

Built from `Wholesale_Accounting_BRD_Django.docx` (BRD v1.0, 28 August 2026).
Django 5.2 LTS · PostgreSQL 16+ · Django Templates (no separate frontend).

---

## Setup

Ten minutes from clone to a running site. If any step fails, `manage.py doctor`
will tell you why.

### 1. Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** (16 recommended) running locally

Everyone runs their own local database. Migrations are shared through git; data
is not. Nobody can break anybody else's data.

### 2. Clone and install

```bash
git clone <repo-url> wams
cd wams

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Open `.env` and set `PGUSER` / `PGPASSWORD` to match your local PostgreSQL.
Everything else can stay as it is for development.

### 4. Create the database

```bash
createdb wams
```

If `createdb` isn't on your PATH, this works too:

```bash
psql -U postgres -c "CREATE DATABASE wams;"
```

### 5. Build the schema

```bash
python manage.py migrate
```

This creates 67 tables, 9 posting-guard triggers, 10 reporting views and 4
reporting functions, then seeds the reference data: chart of accounts, account
mappings, tax codes, currencies, the 2026–2027 fiscal calendar, money accounts,
payment methods, number series and the seven role groups.

### 6. Check your setup

```bash
python manage.py doctor
```

Every line should say PASS. If something fails, the output tells you the fix.

### 7. Create yourself a login and run it

```bash
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in.

A superuser bypasses every permission check, which makes it the wrong account
for testing roles. To check that authorisation actually works, create a second
user in the admin, put them in the **Cashier** group, and confirm they cannot
reach an accounting action.

---

## What already exists

The database schema is **complete and verified** — this is not a starting point
to be filled in, it is a foundation to build screens on.

| Area | Status |
|---|---|
| All models across 10 apps | Built, migrated, verified |
| Reference data | Seeded (chart of accounts, tax, currencies, calendar, roles) |
| Accounting rules | Enforced in the database (see below) |
| Reporting layer | 10 views + 4 functions |
| Authentication and roles | Built — 7 role groups with permissions, login/logout |
| Base template and UI kit | Built — `templates/base.html` and component classes |
| Django admin | Registered for configuration and master data |
| Shared list pattern | Built — `FilteredListView` + `core/list_base.html` |
| Audit trail service | Built — `apps/core/audit.py` |
| Customers and vendors | Built — list, detail, form, deactivate |
| Remaining feature screens | **Not built** — this is the work |

Before designing a model, check whether it already exists. It probably does.

### The rules the database enforces

Nine triggers and 168 CHECK constraints make certain things impossible, whatever
the application code does. You will meet these as errors — that is intentional.

- A journal entry's stored totals must equal the sum of its lines, and debits
  must equal credits (BR-006). Checked at COMMIT, so you may write the header
  before the lines.
- No posting into a closed fiscal period, or with a date outside its period (BR-020).
- No posting to a non-postable parent account or a deactivated account (GL-010).
- An AR or AP line must carry a party (GL-011).
- Posted journals and journal lines are never edited or deleted — reverse instead (BR-004).
- Stock cannot go negative unless policy allows it (BR-017).
- An invoice's open balance must equal total − allocated − credited (BR-009).
- Allocations cannot exceed the payment or the document (BR-008).

If you hit one of these, the fix is almost always in your code, not the schema.
Bring it to Member 1 before changing a constraint.

### Verifying the schema

```bash
# needs a fresh database — it writes posted journals, which are immutable
dropdb wams && createdb wams && python manage.py migrate
python verify_schema.py
```

55 checks. All should pass.

---

## Project layout

```
config/                 settings, root URLs, WSGI/ASGI
apps/
  core/                 company, currency, FX, tax, payment terms, fiscal
                        calendar, numbering, audit trail, shared base models
  accounts/             User, roles
  ledger/               chart of accounts, mappings, journals, posting guards
  parties/              customers, vendors, addresses, contacts
  catalog/              products, categories, units, price lists
  inventory/            warehouses, stock movements, receipts, deliveries
  sales/                orders, invoices, returns, credit notes
  purchases/            orders, bills, returns, debit notes
  payments/             money accounts, payments, allocations, refunds
  reports/              no models — reporting views and functions
verify_schema.py        constraint verification harness
schema_reference.sql    pg_dump of the built schema, for reference
```

## Who owns what

| Member | Apps | Focus |
|---|---|---|
| 1 | core, accounts, parties, catalog | Foundation, identity, master data, shared UI |
| 2 | purchases, inventory | Purchasing and stock | AI feature in the updated version
| 3 | sales | Sales cycle and customer returns |
| 4 | payments, ledger, reports | Posting engine, money, financial statements |

**You run `makemigrations` only for your own apps.** See CONTRIBUTING.md.

## Useful commands

```bash
python manage.py doctor              # check your setup
python manage.py test                # run the test suite
python manage.py migrate             # apply schema changes
python manage.py runserver           # start the dev server
python manage.py shell               # Django shell
python verify_schema.py              # verify the accounting rules (fresh DB)
ruff format . && ruff check .        # format and lint before committing
npm run build:css                    # rebuild static/css/app.css after UI changes
```

When developing against a remote PostgreSQL database, use
`python manage.py runserver --nothreading`. Django's default development server
creates a thread per request, so it cannot reliably reuse a persistent database
connection; the single-threaded option avoids repeating the remote TLS and
connection setup during ordinary page-to-page navigation. This advice is for
local development only—never use `runserver` in production.

### Frontend assets

The application remains full-stack Django: templates are rendered on the
server and there is no JavaScript application to run. Tailwind is a build-time
tool only. A compiled, minified stylesheet is committed, so a normal Python
setup works without Node. Contributors changing templates or design tokens run:

```bash
npm ci
npm run watch:css   # development, or npm run build:css once
```

Production runs `python manage.py collectstatic`; the web server or platform
must serve `STATIC_ROOT` at `/static/`.

The measured performance, security findings, implemented corrections, and
production runbook are recorded in
[`docs/performance-security-quality-audit.md`](docs/performance-security-quality-audit.md).

## Reporting layer

Available to query directly — Member 4 builds screens on top of these.

**Views:** `v_general_ledger`, `v_control_account_balance`, `v_sales_invoice_open`,
`v_purchase_bill_open`, `v_customer_unapplied_credit`, `v_vendor_unapplied_credit`,
`v_inventory_valuation`, `v_tax_transaction`, `v_subledger_reconciliation`,
`v_money_account_activity`

**Functions:** `fn_trial_balance(from, to)`, `fn_ar_ageing(as_of)`,
`fn_ap_ageing(as_of)`, `fn_stock_card(product, warehouse, from, to)`

## Card payments with Stripe (PAY-013)

**Off by default.** With no `STRIPE_SECRET_KEY` the integration is invisible: no
button on the invoice screen, and the webhook refuses everything. Nothing else
in the application changes, and a Stripe receipt can still be entered by hand
using the seeded `STRIPE` payment method.

### How it works

Staff-initiated, not customer-facing. A user opens a posted invoice, clicks
**Create payment link**, and sends the link on to the customer by whatever
channel they already use. When the customer pays, Stripe calls back and the
receipt posts itself against the invoice.

The webhook at `/payments/stripe/webhook/` is the **only** route in this project
reachable without logging in. Its authentication is the Stripe signature; with
no `STRIPE_WEBHOOK_SECRET` set, every request to it is rejected rather than
trusted.

### Switching it on

```bash
# 1. Keys, in .env — test keys for development, never a live key in git
STRIPE_SECRET_KEY=sk_test_...
STRIPE_RETURN_ORIGIN=http://127.0.0.1:8000

# 2. Forward webhooks to the dev server and take the secret it prints
stripe listen --forward-to localhost:8000/payments/stripe/webhook/
# -> STRIPE_WEBHOOK_SECRET=whsec_...
```

### What it does to the books

A processor keeps a cut, so what settles the customer and what reaches the bank
are two different numbers. Payments carry a `fee_txn` for the difference:

```
Customer pays 1,000.00, Stripe keeps 29.30

  Dr  1140  Stripe Clearing              970.70
  Dr  6510  Merchant and Processor Fees   29.30
      Cr  2210  Customer Advances                1,000.00
```

The customer is settled for the gross — that is what clears their balance — and
only the money that genuinely moved reaches the clearing account. When Stripe
pays out, clear 1140 against the bank with an ordinary journal.

The fee field is not Stripe-specific: use it for a wire charge on a vendor
payment too, where the sign flips and the fee is added on top of what the vendor
receives rather than taken out of it.

### When the ledger will not accept the receipt

A webhook can arrive on a day no fiscal period is open, or before anyone has
entered an exchange rate. **The payment is recorded anyway** and the reason is
shown on the invoice screen with a button to finish the posting once the cause
is fixed. This is deliberate: failing the webhook instead would make Stripe
retry for hours, give up, and leave money received with no record of it here.

---

## Deploying

```bash
docker build -t wams .
docker run --env-file .env -p 8000:8000 wams
```

The image runs gunicorn with the settings in `gunicorn.conf.py` and serves its
own static files through WhiteNoise, so nothing else is required in front of
it. Put it behind a reverse proxy or CDN if you want one; both will simply
answer before WhiteNoise does.

**Migrations are not run by the container.** Several replicas starting at once
would race each other, and a schema change should be something a person decides
rather than a side effect of a restart. Run them as a release step:

```bash
python manage.py migrate
```

### Probes

| Path | Answers | Use it for |
|---|---|---|
| `/healthz/` | Is the process alive? Touches nothing, ~1ms | liveness / restart |
| `/readyz/` | Can it reach the database? | readiness / rotation |

Point liveness at `/healthz/` only. A database blip failing a *liveness* check
restarts every replica, which fixes nothing and removes the capacity that was
still working.

### Sizing the workers

The connection budget, not the CPU count, is the constraint here. The Supabase
pooler allows 60 connections in total, and with `CONN_MAX_AGE` above zero each
worker thread can hold one:

    workers x threads  <=  your share of 60

The defaults (2 x 4) use 8. `gunicorn.conf.py` uses threaded workers rather
than sync ones on purpose: with a remote database most requests are waiting on
I/O, and a sync worker would hold an entire process while it did.

### The thing that matters most

A query round trip to the database is **~300ms** from outside its region, and
opening a fresh connection costs **~2.3s**. Both were measured, not estimated.
No amount of query tuning competes with running the application in the same
region as the database - `ap-northeast-2` for the current project. Everything
else in this section is worth doing; this is worth doing first.

---
### Financial statements (RPT-001..RPT-005)

Four screens under **Financials**, gated on `view_financial_reports`:

| Screen | Answers | Spans |
|---|---|---|
| General ledger | every posted line and its source document | filtered |
| Trial balance | opening, movement, closing per account | a range |
| Profit and loss | what was earned and what it cost | a range |
| Balance sheet | what is owned and owed | a date |

All three statements come from `fn_trial_balance` and nothing else. That is
deliberate: they are three presentations of the same balances, and computing
them from three queries is how a balance sheet ends up disagreeing with its own
trial balance. Anything added here should go through
`apps/reports/services.py:account_balances` rather than writing a fourth
definition of "balance".

Two behaviours that look wrong until you know why:

- **Reversed entries are included.** The ledger is append-only, so reversing
  entry A writes a second entry B with the opposite lines and leaves A's lines
  in place. Filtering to `status = 'POSTED'` would drop A but keep B and leave
  every report short by the reversal.
- **The balance sheet carries the year's result.** Until a closing entry moves
  income and expense into reserves, the profit sits in the P&L accounts and the
  statement would be out by exactly that amount. It shows as its own equity
  line and becomes zero on its own once the year is closed.

### The other reports (RPT-006, RPT-007, RPT-008, RPT-013)

| Screen | Answers | Drill-down |
|---|---|---|
| Receivables ageing | who owes us, and how late | invoice, customer |
| Payables ageing | who we owe, and how late | bill, vendor |
| Tax report | tax charged on sales, incurred on purchases | — |
| Money register | one cash or bank account, movement by movement | journal entry |

Inventory valuation already lives under Inventory → Inventory valuation
(`v_inventory_valuation`), so it is not duplicated here.

**Ageing is single-currency, deliberately.** `fn_ar_ageing` and `fn_ap_ageing`
return each document in its own currency with no base equivalent, and a total
that adds dollars to euros would be worse than no total on a report whose whole
job is to be totalled and chased. So currency is a filter, and anything open in
another currency is counted and named underneath — choosing a currency never
hides money.

**The tax report does not net the two sides.** A return is filed as output tax
and input tax; the amount payable is a consequence of those two rather than a
figure in its own right. Non-recoverable input tax is carried separately,
because it is a cost rather than something to reclaim.

### Closing a period (CFG-009, ACC-008, BR-020)

**Financials → Period close**, or any row on the fiscal periods list. The screen
runs a checklist and offers the button.

The checklist separates two kinds of finding, and the distinction is the point:

| | Meaning | Effect |
|---|---|---|
| **Blocks closing** | the arithmetic is wrong | close is refused |
| **Needs a decision** | somebody has to judge it | close is allowed, and the acknowledgement is kept |

Blockers are an open earlier period and a trial balance that does not balance.
Neither is a matter of opinion, and no reason text makes closing over them
right. Warnings are unposted documents dated in the period and a control
account that disagrees with its subledger — both serious, both frequently
older than the period being closed, and making them blockers means one
historical mistake freezes the calendar until somebody unpicks it.

A reason is required either way. It is the only lasting record of who signed
the period off, and it is kept on the period alongside who and when.

**Reopening** needs its own permission, its own reason, and goes in reverse
order — reopening March while April is closed would let a new entry change an
opening balance that has already been signed off. A `LOCKED` period never
reopens; that is the difference between locked and closed.

Enforcement is not only in this code. `wams_journal_period_check()` rejects any
journal entry aimed at a closed period at the database level, so a period that
is closed is closed to everything, not merely to the screens.

## Giving a product its first stock

A newly created product has a quantity of zero, and the **Add product** form has
no quantity field. That is deliberate, not an omission.

Quantity on hand is not stored on the product. It is derived from posted stock
movements, and inventory is simultaneously a control account in the general
ledger — so a number typed into a catalogue form would be stock that no movement
explains and no journal balances. The stock ledger and account 1310 would
disagree from that moment on, and `/reports/reconciliation/` would report the
difference without being able to say where it came from.

There are two supported routes, and which one is right depends on how the stock
actually arrived:

| You are | Use | It posts |
|---|---|---|
| buying the goods | **Goods receipt** (`/inventory/receipts/`) | `Dr Inventory` / `Cr Goods received not invoiced` |
| recording stock you already own | **Stock adjustment**, reason `OPENING` (`/inventory/adjustments/`) | `Dr Inventory` / `Cr Opening balance equity` |

Use the goods receipt whenever a vendor is involved — it leaves the purchase
trail intact and the bill later clears the accrual. Use the opening adjustment
only when migrating stock that predates the system, which is what account 3900
is there to absorb.

A count that finds more than the system expected is a third case and has its own
reason, `COUNT-UP`, which books the difference to inventory adjustment gain
rather than to equity. Reaching for `OPENING` there would bury a real gain in the
opening balance.

## Pending accountant sign-off

These are placeholders (BRD §14.4) and will change. Don't hard-code them.

- Base currency **USD** (OD-02) — cannot change after the first posted transaction
- Standard tax rate **11%** (OD-01)
- Chart of accounts codes and names
- Exempt and zero-rated codes flagged non-recoverable
- Stripe clearing **1140** and processor fees **6510** (PAY-013); `MERCHANT_FEE`
  can be re-pointed from Settings → Account Mappings without a migration

---

## Troubleshooting

**"'charset' meta element should be specified in the '<head>'"** — from
webhint, via the Edge Tools editor extension, and it is wrong. It reads the
template rather than the page: a `{% comment %}` block above `<html>` is body
text to an HTML parser, so it opens an implied `<body>` and reports everything
after it as misplaced. 72 of the 76 files under `templates/` are fragments with
no `<html>` at all, so document-level hints cannot be evaluated there in
principle.

Those two hints are switched off in `.hintrc`. The rule itself is real, so it
is checked where it can be answered — `DocumentHeadTests` in
`apps/accounts/tests/test_auth_pages.py` renders all six auth pages and asserts
the charset and viewport are present and ahead of `<body>`. To lint the real
thing, point webhint at a running URL rather than at the template directory.


**`connection refused` on migrate** — PostgreSQL isn't running, or `PGPORT` in
`.env` is wrong. macOS: `brew services start postgresql`. Linux:
`sudo systemctl start postgresql`.

**`password authentication failed`** — `PGUSER` / `PGPASSWORD` in `.env` don't
match your local PostgreSQL user.

**`permission denied to create extension`** — your PostgreSQL user isn't a
superuser. Either grant it, or create the extensions once by hand:
`psql -d wams -c "CREATE EXTENSION btree_gist; CREATE EXTENSION pg_trgm;"`

**`InconsistentMigrationHistory`** — your database was created before a
migration was reordered. Fastest fix on a dev machine:
`dropdb wams && createdb wams && python manage.py migrate`.

**Conflicting migration numbers after a pull** — see CONTRIBUTING.md; usually
`python manage.py makemigrations --merge`.

**A constraint or trigger rejected my write** — read the error message; it names
the BRD rule. That is the schema doing its job.
