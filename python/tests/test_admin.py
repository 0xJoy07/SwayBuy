"""Admin command integration tests — require TEST_DATABASE_URL (@pytest.mark.db)."""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

pytestmark = pytest.mark.db

SCHEMA = "admin_test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_pool(test_db_url: str):
    pool = ConnectionPool(
        test_db_url,
        open=True,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )
    from db.migrations import migrate
    migrate(pool, SCHEMA)

    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {s}.products ("
                "  id    serial PRIMARY KEY, "
                "  name  text NOT NULL, "
                "  price numeric(12,2) NOT NULL, "
                "  stock integer NOT NULL DEFAULT 0"
                ")"
            ).format(s=sql.Identifier(SCHEMA))
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.products (name, price, stock) "
                "VALUES ('Admin Widget', 25.00, 10)"
            ).format(s=sql.Identifier(SCHEMA))
        )

    yield pool

    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )
    pool.close()


@pytest.fixture(scope="module")
def admin_catalog(admin_pool):
    from db.catalog import CatalogSQL
    return CatalogSQL(
        copy_schema=SCHEMA,
        products_table="products",
        id_col="id",
        name_col="name",
        price_col="price",
        stock_col="stock",
    )


def _pid(pool: ConnectionPool, name: str) -> int:
    with pool.connection() as conn:
        return conn.execute(
            sql.SQL("SELECT id FROM {s}.products WHERE name = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (name,),
        ).fetchone()["id"]


def _place_and_complete_order(
    pool: ConnectionPool, catalog, phone: str, session_id: str
) -> str:
    """Place an order against the test schema and mark it completed."""
    from db.orders_repo import create_pending_action, confirm_pending_action
    pid = _pid(pool, "Admin Widget")
    with pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, session_id, "place_order",
            {
                "session_id": session_id,
                "phone": phone,
                "order_type": "delivery",
                "delivery_address": "1 Admin Lane",
                "items": [{"product_id": pid, "quantity": 1}],
                "pickup_time": None,
                "notes": None,
            },
            ttl_minutes=15,
        )
        result = confirm_pending_action(conn, SCHEMA, token, session_id, catalog)

    order_number = result["order_number"]
    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {s}.orders SET status = 'confirmed' WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.order_status_history "
                "(order_id, old_status, new_status, note, changed_by) "
                "SELECT id, 'pending', 'confirmed', 'test', 'test' "
                "FROM {s}.orders WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        )
    return order_number


# ---------------------------------------------------------------------------
# set-status
# ---------------------------------------------------------------------------

def test_set_status_valid_transition(admin_pool, admin_catalog) -> None:
    from db.orders_repo import transition_order_status

    order_number = _place_and_complete_order(
        admin_pool, admin_catalog, "5000000001", "admin_sess_1"
    )

    with admin_pool.connection() as conn:
        transition_order_status(
            conn, SCHEMA, order_number, "preparing",
            "Moving to kitchen", "admin"
        )
        row = conn.execute(
            sql.SQL("SELECT status FROM {s}.orders WHERE order_number = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (order_number,),
        ).fetchone()

    assert row["status"] == "preparing"


def test_set_status_invalid_transition_raises(admin_pool, admin_catalog) -> None:
    from db.orders_repo import transition_order_status, OrderError

    order_number = _place_and_complete_order(
        admin_pool, admin_catalog, "5000000002", "admin_sess_2"
    )

    with admin_pool.connection() as conn:
        with pytest.raises(OrderError, match="Cannot transition"):
            transition_order_status(
                conn, SCHEMA, order_number, "completed",
                "Jumping straight to completed", "admin"
            )


# ---------------------------------------------------------------------------
# refund-create
# ---------------------------------------------------------------------------

def test_refund_create_valid(admin_pool, admin_catalog) -> None:
    from db.returns_repo import create_refund

    order_number = _place_and_complete_order(
        admin_pool, admin_catalog, "5000000003", "admin_sess_3"
    )

    with admin_pool.connection() as conn:
        result = create_refund(
            conn, SCHEMA, order_number,
            amount=Decimal("10.00"),
        )

    assert result["refund_id"] > 0
    assert result["status"] == "pending"


def test_refund_create_over_value_raises(admin_pool, admin_catalog) -> None:
    from db.returns_repo import create_refund, ReturnError

    order_number = _place_and_complete_order(
        admin_pool, admin_catalog, "5000000004", "admin_sess_4"
    )

    with admin_pool.connection() as conn:
        with pytest.raises(ReturnError, match="exceeds"):
            create_refund(
                conn, SCHEMA, order_number,
                amount=Decimal("99999.00"),
            )


# ---------------------------------------------------------------------------
# return-action mark_received --restock
# ---------------------------------------------------------------------------

def test_return_action_mark_received_restores_stock(admin_pool, admin_catalog) -> None:
    from db.orders_repo import transition_order_status
    from db.returns_repo import request_return, transition_return

    phone = "5000000005"
    session_id = "admin_sess_5"
    order_number = _place_and_complete_order(admin_pool, admin_catalog, phone, session_id)

    # Mark order as completed so return window applies
    with admin_pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {s}.orders SET status = 'completed' WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.order_status_history "
                "(order_id, old_status, new_status, note, changed_by) "
                "SELECT id, 'confirmed', 'completed', 'test', 'test' "
                "FROM {s}.orders WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        )
        oi = conn.execute(
            sql.SQL(
                "SELECT oi.id FROM {s}.order_items oi "
                "JOIN {s}.orders o ON o.id = oi.order_id "
                "WHERE o.order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        ).fetchone()

    pid = _pid(admin_pool, "Admin Widget")
    with admin_pool.connection() as conn:
        before_stock = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]

    with admin_pool.connection() as conn:
        ret = request_return(
            conn, SCHEMA, order_number, phone,
            oi["id"], 1, "changed mind",
            return_window_days=365,
        )

    return_id = ret["return_id"]

    # Approve first
    with admin_pool.connection() as conn:
        transition_return(
            conn, SCHEMA, return_id, "approved", admin_catalog, restock=False
        )

    # Mark received with restock=True
    with admin_pool.connection() as conn:
        transition_return(
            conn, SCHEMA, return_id, "item_received", admin_catalog, restock=True
        )

    with admin_pool.connection() as conn:
        after_stock = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]

    assert after_stock == before_stock + 1


# ---------------------------------------------------------------------------
# refund-set: completed refunds update payment_status on order
# ---------------------------------------------------------------------------

def test_refund_set_completed_updates_payment_status(admin_pool, admin_catalog) -> None:
    from db.returns_repo import create_refund, set_refund_status

    order_number = _place_and_complete_order(
        admin_pool, admin_catalog, "5000000006", "admin_sess_6"
    )

    with admin_pool.connection() as conn:
        refund = create_refund(
            conn, SCHEMA, order_number,
            amount=Decimal("25.00"),
            note="Full refund",
        )

    with admin_pool.connection() as conn:
        set_refund_status(conn, SCHEMA, refund["refund_id"], "completed")
        row = conn.execute(
            sql.SQL(
                "SELECT payment_status FROM {s}.orders WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        ).fetchone()

    assert row["payment_status"] == "refunded"
