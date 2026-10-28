# Shopping Agent — Terminal Backend

A configurable AI shopping/support agent that works **entirely in the terminal**
(no web server, no REST API). It reads a Supabase Postgres store database
(read-only), clones it into a private copy schema, and runs a terminal chat
agent (LangChain + ChatGroq) that can place orders, cancel them, and handle
returns/refunds — **all writes go to the copy, never to the original data**.

```
you> do you have rice?
assistant> I found 3 rice products: Basmati Rice 5kg (INR 499.00, 20 in stock)...
you> order 2 of the first one, deliver to 12 MG Road. My number is 9876543210
assistant> Here is your order summary … please confirm in the terminal prompt.
Confirm? (y/n): y          ← the CLI asks, NEVER the model
Order ORD-20261028-A1B2 placed — status: pending
```

## Layout

```
python/
  main.py            Typer entrypoint: check, scan, confirm-mapping, clone,
                     migrate, diff, reset-copy, chat, admin …
  config.py          .env loading / validation / masking (secrets never printed)
  cli/
    check_cli.py     env + connections + Groq key + setup state
    setup_cli.py     scan, confirm-mapping, clone, migrate, diff, reset-copy
    chat_cli.py      terminal chat loop (y/n confirmation owned here)
    admin_cli.py     orders, order, set-status, returns, return-action,
                     refund-create, refund-set
  db/
    pools.py         the ONLY module that opens connections; source pool is
                     opened SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY
    scanner.py       introspection + mapping proposal/validation
    cloner.py        clone source tables into the copy schema (own sequences)
    migrations.py    9 record tables, CHECK constraints, indexes
    catalog.py       fixed parameterized SQL over the mapped product table
    orders_repo.py   atomic order placement / cancellation / transitions
    returns_repo.py  return requests and refunds
    chat_repo.py     chat history (copy schema only)
  agent/
    context.py       AgentContext dataclass (copy_pool, catalog_sql, session_id)
    prompts.py       build_system_prompt — hard rules injected here
    main_agent.py    build_agent / run_turn — CompiledStateGraph via create_agent
    stock_agent.py   search_products, check_availability, find_alternatives
    review_agent.py  get_review_summary (only when reviews mapping exists)
    order_agent.py   prepare_order, prepare_cancel, get_order_status,
                     request_return, get_return_status
  tests/             pytest suite (DB tests need TEST_DATABASE_URL)
  state/             mapping.proposed.json / mapping.json  (git-ignored)
```

## The non-negotiables (enforced in code and tests)

1. **Source is READ ONLY** — the source pool configures every session with
   `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`; scanner/cloner read
   through it and nothing else ever imports it.
   `tests/test_no_source_access.py` AST-greps every copy-only module for source
   pool references and always runs in CI without any database.
2. **Copy schema is never `public` and never equals the source schema** —
   validated in `config.py` and asserted again in `db/pools.py`.
3. **The LLM never writes SQL** — the model only calls tools; every table and
   column name comes from the confirmed mapping and is inserted with
   `psycopg.sql.Identifier`, always schema-qualified.
4. **All agent facts come from tool results** — tools return empty results /
   `not_found` honestly; the system prompt forbids inventing data.
5. **Confirmations are CLI-owned** — `prepare_order` / `prepare_cancel` only
   create a pending_actions row and return `requires_confirmation=True`;
   the terminal prompts `Confirm? (y/n)`. The model never confirms.
   `session_id` is injected by the CLI via `AgentContext`, never a tool argument.
6. **Money is `numeric(12,2)`** and every status/enum has a database CHECK
   constraint.

## Dependencies

Install with:

```
pip install -r requirements.txt
```

Key direct dependencies:

| Package | Purpose |
|---|---|
| `psycopg[binary,pool]>=3.2` | Postgres driver (psycopg 3) |
| `psycopg_pool` | bundled with psycopg[pool] |
| `langchain>=1.0` | agent graph via `create_agent` |
| `langchain-groq>=1.1` | `ChatGroq` model integration |
| `langchain-core>=1.6` | `StructuredTool`, `BaseMessage` |
| `groq` | direct Groq client (check + scan --use-llm) |
| `pydantic>=2.7` | tool input validation |
| `typer>=0.12` | CLI |
| `rich>=13` | terminal output |
| `python-dotenv>=1.0` | .env loading |
| `pytest>=8.0` | test runner |

## Setup

Requirements: Python 3.11+, a Postgres database to clone from (Supabase works),
and a Groq API key for `chat`.

```
cd python
copy .env.example .env    # Windows
```

Fill in `.env`:

| Variable | Required | Meaning |
|---|---|---|
| `SOURCE_DATABASE_URL` | yes | Supabase Postgres URL (read-only usage). Use the direct / session-mode pooler (port 5432). |
| `COPY_DATABASE_URL` | no | Separate DB for the copy; defaults to the source database |
| `SOURCE_SCHEMA` | no | Source schema (default `public`) |
| `COPY_SCHEMA` | no | Copy schema (default `agent_copy`; must not be `public`) |
| `GROQ_API_KEY` | for `chat` | Groq API key — never logged or sent to any other endpoint |
| `GROQ_MODEL` | no | Default `llama-3.3-70b-versatile` (must support tool calling) |
| `STORE_NAME` | no | Shown to customers in chat |
| `CURRENCY` | no | Display currency (no conversion performed) |
| `RETURN_WINDOW_DAYS` | no | Days after completion that returns are accepted (default 7) |
| `CONFIRMATION_TTL_MINUTES` | no | Pending order token expiry (default 15) |
| `TEST_DATABASE_URL` | for DB tests | Throwaway Postgres DB for the pytest DB suite |

## Command order

```
python main.py check                    # env + connections + Groq key + setup state
python main.py scan                     # read-only introspection → state/mapping.proposed.json
python main.py scan --use-llm           # same, with LLM refinement (needs GROQ_API_KEY)
python main.py confirm-mapping          # interactive review → state/mapping.json
python main.py clone                    # copy catalog tables (own sequences, row-count check)
python main.py clone --only-mapped      # clone only the tables referenced in the mapping
python main.py migrate                  # create the 9 record tables in the copy schema
python main.py diff                     # source vs copy: per-product stock + record counts
python main.py chat                     # terminal chat agent (copy only, needs GROQ_API_KEY)
python main.py reset-copy               # drop the copy schema (type its name to confirm)

python main.py admin orders             # list all orders
python main.py admin orders --status pending
python main.py admin order ORD-...      # full order detail with items + history
python main.py admin set-status ORD-... confirmed --note "Verified payment"
python main.py admin returns            # list return requests
python main.py admin returns --status requested
python main.py admin return-action 7 approve
python main.py admin return-action 7 mark_received --restock
python main.py admin refund-create ORD-... --amount 9.99 --note "Partial refund"
python main.py admin refund-set 3 completed
```

`clone`, `migrate` are safe to re-run where the schema is empty (idempotent on
an already-migrated copy). `reset-copy` drops everything.

## Tests

```
# Always-run unit tests (no database needed)
python -m pytest tests -q

# Full DB + integration suite
set TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/testdb
python -m pytest tests -q
```

The DB tests create and drop isolated throwaway schemas automatically.
Covered scenarios:

- Source pool enforces `READ ONLY`; copy pool is writable
- Clone: row counts match, source untouched, sequences belong to copy schema, FK enforced
- Migrations: all 9 tables created, idempotent, CHECK constraints enforced
  (delivery address, takeaway pickup_time, quantity > 0, status enums)
- Repos: atomic placement, concurrent last-unit race (threading), stock deduction
  and restore, expired / reused / wrong-session tokens, invalid status transitions,
  paid-order cancel creates `refund_pending` + refund row, return window / quantity
  guards, refund over-value guard, chat history limit
- Tools: Pydantic validators (phone, delivery/takeaway, quantity), tool schema
  never exposes `_ctx`, `make_review_tools` returns empty list without reviews table
- Admin: valid/invalid status transitions, refund over-value, mark_received restores stock,
  completed refund flips `payment_status` to `refunded`
- Smoke: one full agent turn (requires `TEST_DATABASE_URL` + `GROQ_API_KEY`)

## Notes

- Refunds are recorded statuses only (no payment gateway). A refund row can
  never exceed the unrefunded value of the order; when all refunds for an order
  reach `completed` or `failed`, `payment_status` is set to `refunded`.
- Cancelled paid orders automatically get `refund_pending` status plus a
  pending refund row.
- Out-of-stock products can be searched but never ordered;
  `find_alternatives` returns in-stock items in the same category within ±35%
  of the original price.
- `session_id` is a UUID generated per `python main.py chat` invocation — it
  scopes pending tokens and chat history. `/new` inside the chat loop starts a
  fresh session.
