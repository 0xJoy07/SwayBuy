# Shopping Agent - Terminal Backend

A configurable AI shopping/support agent that works **entirely in the terminal**
(no web server, no REST API). It reads a Supabase Postgres store database
(read-only!), clones it into a private copy schema, and runs a terminal chat
agent (LangChain + ChatGroq) that can place orders, cancel them and handle
returns/refunds - **all writes go to the copy, never to the original data**.

```
you> do you have rice?
assistant> I found 3 rice products: Basmati Rice 5kg (INR 499.00, 20 in stock)...
you> order 2 of the first one, deliver to 12 MG Road, Pune. I'm Riya, 9876543210
assistant> Here is your order summary ... please confirm in the terminal prompt.
Confirm? (y/n): y          <- the CLI asks, NEVER the model
Order ORD-20261028-A1B2 placed. Total INR 998.00.
```

## Layout

```
python/
  main.py            Typer entrypoint: check, scan, confirm-mapping, clone,
                     migrate, diff, reset-copy, chat, admin
  config.py          .env loading/validation/masking (secrets never printed)
  cli/               terminal commands (chat loop, admin, check, setup)
  db/
    pools.py         the ONLY module that opens connections; source pool is
                     opened with default_transaction_read_only=on
    scanner.py       introspection + mapping proposal/validation (STEP 1)
    cloner.py        clone source tables into the copy schema (STEP 2)
    migrations.py    record tables in the copy schema, CHECK-guarded (STEP 3)
    catalog.py       fixed helper SQL over the mapped product table
    orders_repo.py   atomic order placement / cancellation / transitions
    returns_repo.py  return requests and refunds (recorded statuses only)
    chat_repo.py     chat history (copy schema only)
  agent/             tools + prompts + agent runner (copy pool only)
  tests/             pytest suite (DB tests need TEST_DATABASE_URL)
  state/             mapping.proposed.json / mapping.json (gitignored)
```

## The non-negotiables (enforced in code and tests)

1. **Source is READ ONLY** - the source pool sets
   `default_transaction_read_only=on`; scanner/cloner read through it and
   nothing else ever imports it. A structural test
   (`tests/test_no_source_access.py`) greps every copy-only module for source
   access, and the DB tests assert a checksum of the source is unchanged after
   a full order/return/refund scenario.
2. **Copy schema is never `public` and never equals the source schema** -
   validated in `config.py` and asserted again in `db/pools.py`.
3. **The LLM never writes SQL** - the model only calls tools; every table and
   column name comes from the confirmed mapping and is inserted with
   `psycopg.sql.Identifier`, always schema-qualified.
4. **All agent facts come from tool results** - tools return empty results /
   `not_found` honestly; the system prompt forbids inventing data.
5. **Confirmations are CLI-owned** - `prepare_order` / `prepare_cancel` only
   create a pending action; the terminal asks `Confirm? (y/n)`. The model never
   confirms, and `session_id` is injected by the CLI (never a tool argument).
6. **Money is `numeric(12,2)`** and every status/enum is CHECK-constrained.

## Setup

Requirements: Python 3.11+, a Postgres database you can clone from
(Supabase works), and a Groq API key for `chat`.

1. `cd python`
2. `copy .env.example .env` and fill in:

   | Variable | Meaning |
   |---|---|
   | `SOURCE_DATABASE_URL` | your Supabase Postgres URL (read-only usage) |
   | `COPY_DATABASE_URL` | optional; defaults to the source database |
   | `SOURCE_SCHEMA` | source schema (default `public`) |
   | `COPY_SCHEMA` | copy schema (default `agent_copy`; never `public`) |
   | `GROQ_API_KEY` | needed for `chat` and `scan --use-llm` |
   | `GROQ_MODEL` | default `llama-3.3-70b-versatile` |
   | `STORE_NAME`, `CURRENCY` | display settings |
   | `RETURN_WINDOW_DAYS`, `CONFIRMATION_TTL_MINUTES` | policy settings |

## Command order

```
python main.py check                 # env + connections + setup state
python main.py scan                  # read-only introspection -> mapping.proposed.json
python main.py confirm-mapping       # interactive review -> state/mapping.json
python main.py clone                 # copy the catalog tables (own sequences!)
python main.py migrate               # create the record tables in the copy
python main.py diff                  # source vs copy: stock + record counts
python main.py chat                  # the terminal agent (copy only)
python main.py admin orders          # inspect orders/returns/refunds
python main.py reset-copy            # drop the copy schema (type the name)
```

`migrate`, `clone` and `migrate` again are safe to re-run (idempotent).

## Tests

```
python -m pytest tests -q                       # unit tests run anywhere
set TEST_DATABASE_URL=postgresql://postgres:SECRET@localhost:5432/postgres
python -m pytest tests -q                       # + full DB scenario suite
```

The DB tests create throwaway `src_*` / `cpy_*` schemas and drop them again.
Covered: source untouched, clone row counts/own sequences/FK recreation, atomic
placement incl. concurrent last-unit races, token security (expired / reused /
wrong session), status transition maps, return window & quantity rules, refund
caps, honest tool results.

## Notes

- Refunds are recorded statuses only (no payment gateway): a refund row can
  never exceed the unrefunded value of returned items; when processed refunds
  cover the whole order, `payment_status` becomes `refunded`.
- Cancelled paid orders get `refund_pending` plus a pending refund row.
- Out-of-stock products can be searched but never ordered; `find_alternatives`
  suggests in-stock items of the same category within +/-35% of the price.
