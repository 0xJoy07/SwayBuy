"""Repository integration tests — require TEST_DATABASE_URL (@pytest.mark.db)."""
from __future__ import annotations

import threading
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCHEMA = "repo_test"


@pytest.fixture(scope="module")
def repo_pool(test_db_url: str):
    pool = ConnectionPool(
        test_db_url,
        open=True,
        min_size=2,
        max_size=8,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )
    from db.migrations import migrate
    from db.catalog import CatalogSQL

    migrate(pool, SCHEMA)

    # Seed catalog products table
    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {s}.products ("
                "  id    serial PRIMARY KEY, "
                "  name  text   NOT NULL, "
                "  price numeric(12,2) NOT NULL, "
                "  stock integer NOT NULL DEFAULT 0, "
                "  category text"
                ")"
            ).format(s=sql.Identifier(SCHEMA))
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.products (name, price, stock, category) VALUES "
                "('Widget A', 10.00, 5, 'widgets'), "
                "('Widget B', 12.00, 1, 'widgets'), "
                "('Gadget X', 50.00, 0, 'gadgets') "
                "ON CONFLICT DO NOTHING"
            ).format(s=sql.Identifier(SCHEMA))
        )

    yield pool

    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )
    pool.close()


@pytest.fixture(scope="module")
def catalog(repo_pool) -> "CatalogSQL":
    from db.catalog import CatalogSQL
    return CatalogSQL(
        copy_schema=SCHEMA,
        products_table="products",
        id_col="id",
        name_col="name",
        price_col="price",
        stock_col="stock",
        category_col="category",
    )


def _product_id(pool: ConnectionPool, name: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            sql.SQL("SELECT id FROM {s}.products WHERE name = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (name,),
        ).fetchone()
    return row["id"]


def _place_order_payload(session_id: str, phone: str, product_id: int, qty: int = 1) -> dict:
    return {
        "session_id": session_id,
        "phone": phone,
        "order_type": "delivery",
        "delivery_address": "1 Test Street",
        "items": [{"product_id": product_id, "quantity": qty}],
    }


# ---------------------------------------------------------------------------
# Catalog tests
# ---------------------------------------------------------------------------

def test_check_availability_unknown_ref(repo_pool, catalog) -> None:
    with repo_pool.connection() as conn:
        result = catalog.check_availability(conn, 99999)
    assert result is None


def test_check_availability_out_of_stock(repo_pool, catalog) -> None:
    pid = _product_id(repo_pool, "Gadget X")
    with repo_pool.connection() as conn:
        result = catalog.check_availability(conn, pid)
    assert result is not None
    assert result["in_stock"] is False


def test_search_returns_empty_for_no_match(repo_pool, catalog) -> None:
    with repo_pool.connection() as conn:
        results = catalog.search(conn, "xyzzy_no_match_ever")
    assert results == []


def test_deduct_stock_returns_none_on_insufficient(repo_pool, catalog) -> None:
    pid = _product_id(repo_pool, "Gadget X")  # stock = 0
    with repo_pool.connection() as conn:
        result = catalog.deduct_stock(conn, pid, 1)
    assert result is None


# ---------------------------------------------------------------------------
# Pending-action token tests
# ---------------------------------------------------------------------------

def test_expired_token_rejected(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action, OrderError

    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, "sess_exp", "place_order",
            _place_order_payload("sess_exp", "9000000001", _product_id(repo_pool, "Widget A")),
            ttl_minutes=-1,   # already expired
        )

    with repo_pool.connection() as conn:
        with pytest.raises(OrderError, match="expired"):
            confirm_pending_action(conn, SCHEMA, token, "sess_exp", catalog)


def test_wrong_session_token_rejected(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action, OrderError

    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, "sess_A", "place_order",
            _place_order_payload("sess_A", "9000000002", _product_id(repo_pool, "Widget A")),
            ttl_minutes=15,
        )

    with repo_pool.connection() as conn:
        with pytest.raises(OrderError, match="does not belong"):
            confirm_pending_action(conn, SCHEMA, token, "sess_B", catalog)


def test_consumed_token_rejected(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action, OrderError

    pid = _product_id(repo_pool, "Widget A")

    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, "sess_cons", "place_order",
            _place_order_payload("sess_cons", "9000000003", pid),
            ttl_minutes=15,
        )
        confirm_pending_action(conn, SCHEMA, token, "sess_cons", catalog)

    with repo_pool.connection() as conn:
        with pytest.raises(OrderError, match="already been used"):
            confirm_pending_action(conn, SCHEMA, token, "sess_cons", catalog)


# ---------------------------------------------------------------------------
# Place order
# ---------------------------------------------------------------------------

def test_place_order_deducts_stock(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action

    pid = _product_id(repo_pool, "Widget B")
    with repo_pool.connection() as conn:
        before = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]

    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, "sess_place", "place_order",
            _place_order_payload("sess_place", "9000000010", pid, qty=1),
            ttl_minutes=15,
        )
        result = confirm_pending_action(conn, SCHEMA, token, "sess_place", catalog)

    with repo_pool.connection() as conn:
        after = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]

    assert after == before - 1
    assert "order_number" in result


def test_cancel_restores_stock(repo_pool, catalog) -> None:
    from db.orders_repo import (
        create_pending_action, confirm_pending_action, transition_order_status
    )

    pid = _product_id(repo_pool, "Widget A")

    # Place
    with repo_pool.connection() as conn:
        before = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]
        token = create_pending_action(
            conn, SCHEMA, "sess_cancel", "place_order",
            _place_order_payload("sess_cancel", "9000000020", pid, qty=1),
            ttl_minutes=15,
        )
        placed = confirm_pending_action(conn, SCHEMA, token, "sess_cancel", catalog)

    order_number = placed["order_number"]

    # Cancel
    with repo_pool.connection() as conn:
        token2 = create_pending_action(
            conn, SCHEMA, "sess_cancel", "cancel_order",
            {"order_number": order_number, "phone": "9000000020"},
            ttl_minutes=15,
        )
        confirm_pending_action(conn, SCHEMA, token2, "sess_cancel", catalog)

    with repo_pool.connection() as conn:
        after = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]

    assert after == before


# ---------------------------------------------------------------------------
# Concurrent last-unit order — exactly one succeeds, stock stays >= 0
# ---------------------------------------------------------------------------

def test_concurrent_last_unit_exactly_one_wins(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action, OrderError

    # Use Widget B which may have 0 stock after previous test — reset to 1
    pid = _product_id(repo_pool, "Widget B")
    with repo_pool.connection() as conn:
        conn.execute(
            sql.SQL("UPDATE {s}.products SET stock = 1 WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        )

    tokens: list[uuid.UUID] = []
    phones = ["8880000001", "8880000002"]
    for i, phone in enumerate(phones):
        with repo_pool.connection() as conn:
            t = create_pending_action(
                conn, SCHEMA, f"sess_conc_{i}", "place_order",
                _place_order_payload(f"sess_conc_{i}", phone, pid, qty=1),
                ttl_minutes=15,
            )
            tokens.append(t)

    successes: list[str] = []
    failures: list[str] = []
    lock = threading.Lock()

    def attempt(token: uuid.UUID, session_id: str) -> None:
        try:
            pool2 = ConnectionPool(
                repo_pool.conninfo,
                open=True,
                min_size=1,
                max_size=1,
                kwargs={"row_factory": psycopg.rows.dict_row},
            )
            with pool2.connection() as conn:
                result = confirm_pending_action(conn, SCHEMA, token, session_id, catalog)
            pool2.close()
            with lock:
                successes.append(result["order_number"])
        except Exception as exc:
            with lock:
                failures.append(str(exc))

    threads = [
        threading.Thread(target=attempt, args=(tokens[i], f"sess_conc_{i}"))
        for i in range(2)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(successes) == 1, f"Expected 1 success, got {successes}"
    assert len(failures) == 1

    with repo_pool.connection() as conn:
        stock = conn.execute(
            sql.SQL("SELECT stock FROM {s}.products WHERE id = %s").format(
                s=sql.Identifier(SCHEMA)
            ),
            (pid,),
        ).fetchone()["stock"]
    assert stock >= 0


# ---------------------------------------------------------------------------
# Invalid status transition
# ---------------------------------------------------------------------------

def test_invalid_transition_raises(repo_pool, catalog) -> None:
    from db.orders_repo import (
        create_pending_action, confirm_pending_action, transition_order_status, OrderError
    )

    pid = _product_id(repo_pool, "Widget A")
    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, "sess_trans", "place_order",
            _place_order_payload("sess_trans", "9000000030", pid),
            ttl_minutes=15,
        )
        placed = confirm_pending_action(conn, SCHEMA, token, "sess_trans", catalog)

    with repo_pool.connection() as conn:
        with pytest.raises(OrderError, match="Cannot transition"):
            transition_order_status(
                conn, SCHEMA, placed["order_number"],
                "completed", "jump straight to completed", "test"
            )


# ---------------------------------------------------------------------------
# Cancel paid order creates refund_pending + refund row
# ---------------------------------------------------------------------------

def test_cancel_paid_order_creates_refund(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action

    pid = _product_id(repo_pool, "Widget A")
    phone = "9000000040"

    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, "sess_paid", "place_order",
            _place_order_payload("sess_paid", phone, pid),
            ttl_minutes=15,
        )
        placed = confirm_pending_action(conn, SCHEMA, token, "sess_paid", catalog)

    order_number = placed["order_number"]

    # Mark as paid
    with repo_pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {s}.orders SET payment_status = 'paid' "
                "WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        )

    # Cancel
    with repo_pool.connection() as conn:
        token2 = create_pending_action(
            conn, SCHEMA, "sess_paid", "cancel_order",
            {"order_number": order_number, "phone": phone},
            ttl_minutes=15,
        )
        result = confirm_pending_action(conn, SCHEMA, token2, "sess_paid", catalog)

    assert result["status"] == "refund_pending"

    with repo_pool.connection() as conn:
        refund = conn.execute(
            sql.SQL(
                "SELECT r.id FROM {s}.refunds r "
                "JOIN {s}.orders o ON o.id = r.order_id "
                "WHERE o.order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        ).fetchone()

    assert refund is not None


# ---------------------------------------------------------------------------
# Return repo
# ---------------------------------------------------------------------------

def _create_completed_order(pool: ConnectionPool, catalog, phone: str, pid: int) -> str:
    """Place an order and mark it completed; return the order_number."""
    from db.orders_repo import create_pending_action, confirm_pending_action
    with pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, f"sess_ret_{phone}", "place_order",
            _place_order_payload(f"sess_ret_{phone}", phone, pid),
            ttl_minutes=15,
        )
        result = confirm_pending_action(conn, SCHEMA, token, f"sess_ret_{phone}", catalog)

    order_number = result["order_number"]
    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {s}.orders SET status = 'completed' WHERE order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        )
    return order_number


def test_return_beyond_window_raises(repo_pool, catalog) -> None:
    from db.returns_repo import request_return, ReturnError

    pid = _product_id(repo_pool, "Widget A")
    phone = "7000000001"
    order_number = _create_completed_order(repo_pool, catalog, phone, pid)

    # Get order_item_id
    with repo_pool.connection() as conn:
        oi = conn.execute(
            sql.SQL(
                "SELECT oi.id FROM {s}.order_items oi "
                "JOIN {s}.orders o ON o.id = oi.order_id "
                "WHERE o.order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        ).fetchone()

    with repo_pool.connection() as conn:
        with pytest.raises(ReturnError, match="window"):
            request_return(
                conn, SCHEMA, order_number, phone,
                oi["id"], 1, "changed mind",
                return_window_days=0,   # window already closed
            )


def test_return_over_quantity_raises(repo_pool, catalog) -> None:
    from db.returns_repo import request_return, ReturnError

    pid = _product_id(repo_pool, "Widget A")
    phone = "7000000002"
    order_number = _create_completed_order(repo_pool, catalog, phone, pid)

    with repo_pool.connection() as conn:
        oi = conn.execute(
            sql.SQL(
                "SELECT oi.id FROM {s}.order_items oi "
                "JOIN {s}.orders o ON o.id = oi.order_id "
                "WHERE o.order_number = %s"
            ).format(s=sql.Identifier(SCHEMA)),
            (order_number,),
        ).fetchone()

    with repo_pool.connection() as conn:
        with pytest.raises(ReturnError, match="Cannot return"):
            request_return(
                conn, SCHEMA, order_number, phone,
                oi["id"], 999, "want too many back",
                return_window_days=30,
            )


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def test_refund_over_value_raises(repo_pool, catalog) -> None:
    from db.orders_repo import create_pending_action, confirm_pending_action
    from db.returns_repo import create_refund, ReturnError

    pid = _product_id(repo_pool, "Widget A")
    phone = "6000000001"

    with repo_pool.connection() as conn:
        token = create_pending_action(
            conn, SCHEMA, f"sess_rfnd_{phone}", "place_order",
            _place_order_payload(f"sess_rfnd_{phone}", phone, pid),
            ttl_minutes=15,
        )
        placed = confirm_pending_action(conn, SCHEMA, token, f"sess_rfnd_{phone}", catalog)

    with repo_pool.connection() as conn:
        with pytest.raises(ReturnError, match="exceeds"):
            create_refund(
                conn, SCHEMA, placed["order_number"],
                amount=Decimal("99999.00"),
            )


# ---------------------------------------------------------------------------
# Chat repo
# ---------------------------------------------------------------------------

def test_chat_save_and_retrieve(repo_pool) -> None:
    from db.chat_repo import save_message, get_history

    sid = "chat_sess_001"
    with repo_pool.connection() as conn:
        save_message(conn, SCHEMA, sid, "human", "Hello there")
        save_message(conn, SCHEMA, sid, "ai", "Hi! How can I help?")
        history = get_history(conn, SCHEMA, sid, limit=20)

    assert len(history) == 2
    assert history[0]["role"] == "human"
    assert history[1]["role"] == "ai"
    assert history[1]["content"] == "Hi! How can I help?"


def test_chat_history_limit(repo_pool) -> None:
    from db.chat_repo import save_message, get_history

    sid = "chat_sess_002"
    with repo_pool.connection() as conn:
        for i in range(25):
            save_message(conn, SCHEMA, sid, "human", f"msg {i}")
        history = get_history(conn, SCHEMA, sid, limit=20)

    assert len(history) == 20
