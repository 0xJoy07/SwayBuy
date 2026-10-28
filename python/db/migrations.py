"""Create all record tables in the copy schema.

All DDL runs inside a single transaction so partial migrations never persist.
Every table and column name is schema-qualified via psycopg.sql.Identifier.
"""
from __future__ import annotations

from psycopg import sql
from psycopg_pool import ConnectionPool


def migrate(copy_pool: ConnectionPool, copy_schema: str) -> None:
    """Create all 9 record tables idempotently in one transaction."""
    s = sql.Identifier(copy_schema)

    with copy_pool.connection() as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s)
        )

        # 1. catalog_mapping — stores confirmed column mapping snapshot
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id            serial      PRIMARY KEY, "
            "  recorded_at   timestamptz NOT NULL DEFAULT now(), "
            "  mapping_json  jsonb       NOT NULL"
            ")"
        ).format(s=s, t=sql.Identifier("catalog_mapping")))

        # 2. customers
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id           serial      PRIMARY KEY, "
            "  phone        text        NOT NULL UNIQUE, "
            "  name         text, "
            "  created_at   timestamptz NOT NULL DEFAULT now()"
            ")"
        ).format(s=s, t=sql.Identifier("customers")))

        # 3. orders
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id               serial         PRIMARY KEY, "
            "  order_number     text           NOT NULL UNIQUE, "
            "  session_id       text           NOT NULL, "
            "  customer_id      integer        NOT NULL REFERENCES {s}.{cust}(id), "
            "  order_type       text           NOT NULL "
            "    CHECK (order_type IN ('delivery', 'takeaway')), "
            "  status           text           NOT NULL DEFAULT 'pending' "
            "    CHECK (status IN ('pending', 'confirmed', 'preparing', "
            "                      'ready', 'out_for_delivery', 'completed', "
            "                      'cancelled', 'refund_pending')), "
            "  payment_status   text           NOT NULL DEFAULT 'unpaid' "
            "    CHECK (payment_status IN ('unpaid', 'paid', 'refund_pending', 'refunded')), "
            "  delivery_address text, "
            "  pickup_time      timestamptz, "
            "  total_amount     numeric(12,2)  NOT NULL, "
            "  notes            text, "
            "  created_at       timestamptz    NOT NULL DEFAULT now(), "
            "  updated_at       timestamptz    NOT NULL DEFAULT now(), "
            "  CONSTRAINT delivery_needs_address "
            "    CHECK (order_type <> 'delivery' OR delivery_address IS NOT NULL), "
            "  CONSTRAINT takeaway_needs_pickup_time "
            "    CHECK (order_type <> 'takeaway' OR pickup_time IS NOT NULL)"
            ")"
        ).format(s=s, t=sql.Identifier("orders"),
                 cust=sql.Identifier("customers")))

        # 4. order_items
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id               serial        PRIMARY KEY, "
            "  order_id         integer       NOT NULL REFERENCES {s}.{ord}(id), "
            "  product_id       integer       NOT NULL, "
            "  product_name     text          NOT NULL, "
            "  unit_price       numeric(12,2) NOT NULL, "
            "  quantity         integer       NOT NULL CHECK (quantity > 0), "
            "  line_total       numeric(12,2) NOT NULL"
            ")"
        ).format(s=s, t=sql.Identifier("order_items"),
                 ord=sql.Identifier("orders")))

        # 5. order_status_history
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id           serial      PRIMARY KEY, "
            "  order_id     integer     NOT NULL REFERENCES {s}.{ord}(id), "
            "  old_status   text, "
            "  new_status   text        NOT NULL "
            "    CHECK (new_status IN ('pending', 'confirmed', 'preparing', "
            "                         'ready', 'out_for_delivery', 'completed', "
            "                         'cancelled', 'refund_pending')), "
            "  note         text, "
            "  changed_by   text        NOT NULL, "
            "  changed_at   timestamptz NOT NULL DEFAULT now()"
            ")"
        ).format(s=s, t=sql.Identifier("order_status_history"),
                 ord=sql.Identifier("orders")))

        # 6. pending_actions
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(), "
            "  session_id   text        NOT NULL, "
            "  action_type  text        NOT NULL "
            "    CHECK (action_type IN ('place_order', 'cancel_order')), "
            "  payload      jsonb       NOT NULL, "
            "  expires_at   timestamptz NOT NULL, "
            "  consumed     boolean     NOT NULL DEFAULT false, "
            "  created_at   timestamptz NOT NULL DEFAULT now()"
            ")"
        ).format(s=s, t=sql.Identifier("pending_actions")))

        # 7. return_requests
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id               serial        PRIMARY KEY, "
            "  order_id         integer       NOT NULL REFERENCES {s}.{ord}(id), "
            "  order_item_id    integer       NOT NULL REFERENCES {s}.{oi}(id), "
            "  customer_id      integer       NOT NULL REFERENCES {s}.{cust}(id), "
            "  quantity         integer       NOT NULL CHECK (quantity > 0), "
            "  reason           text, "
            "  status           text          NOT NULL DEFAULT 'requested' "
            "    CHECK (status IN ('requested', 'approved', 'rejected', "
            "                      'item_received', 'closed')), "
            "  requested_at     timestamptz   NOT NULL DEFAULT now(), "
            "  updated_at       timestamptz   NOT NULL DEFAULT now()"
            ")"
        ).format(s=s, t=sql.Identifier("return_requests"),
                 ord=sql.Identifier("orders"),
                 oi=sql.Identifier("order_items"),
                 cust=sql.Identifier("customers")))

        # 8. refunds
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id               serial        PRIMARY KEY, "
            "  order_id         integer       NOT NULL REFERENCES {s}.{ord}(id), "
            "  return_request_id integer      REFERENCES {s}.{rr}(id), "
            "  amount           numeric(12,2) NOT NULL CHECK (amount > 0), "
            "  status           text          NOT NULL DEFAULT 'pending' "
            "    CHECK (status IN ('pending', 'processing', 'completed', 'failed')), "
            "  note             text, "
            "  created_at       timestamptz   NOT NULL DEFAULT now(), "
            "  updated_at       timestamptz   NOT NULL DEFAULT now()"
            ")"
        ).format(s=s, t=sql.Identifier("refunds"),
                 ord=sql.Identifier("orders"),
                 rr=sql.Identifier("return_requests")))

        # 9. chat_messages
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {s}.{t} ("
            "  id           serial      PRIMARY KEY, "
            "  session_id   text        NOT NULL, "
            "  role         text        NOT NULL "
            "    CHECK (role IN ('human', 'ai', 'tool')), "
            "  content      text        NOT NULL, "
            "  created_at   timestamptz NOT NULL DEFAULT now()"
            ")"
        ).format(s=s, t=sql.Identifier("chat_messages")))

        # Indexes
        _create_index(conn, s, "orders", "idx_orders_order_number", ["order_number"])
        _create_index(conn, s, "orders", "idx_orders_session_id",   ["session_id"])
        _create_index(conn, s, "orders", "idx_orders_status",       ["status"])
        _create_index(conn, s, "customers", "idx_customers_phone",  ["phone"])
        _create_index(conn, s, "chat_messages", "idx_chat_session",  ["session_id"])
        _create_index(conn, s, "pending_actions", "idx_pending_session", ["session_id"])


def _create_index(
    conn,
    schema_ident: sql.Identifier,
    table: str,
    index_name: str,
    columns: list[str],
) -> None:
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    conn.execute(
        sql.SQL(
            "CREATE INDEX IF NOT EXISTS {idx} ON {s}.{t} ({cols})"
        ).format(
            idx=sql.Identifier(index_name),
            s=schema_ident,
            t=sql.Identifier(table),
            cols=col_idents,
        )
    )


def record_tables_applied(copy_pool: ConnectionPool) -> bool:
    """Return True if the orders table exists in the copy schema."""
    import config
    settings = config.load_settings(require_source=False)
    try:
        with copy_pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'orders'",
                (settings.copy_schema,),
            ).fetchone()
        return row is not None
    except Exception:
        return False
