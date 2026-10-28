"""Migration integration tests — require TEST_DATABASE_URL (@pytest.mark.db)."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mig_schema() -> str:
    return "mig_test"


@pytest.fixture()
def fresh_copy_pool(test_db_url: str, mig_schema: str):
    """Pool connected to a throwaway schema; dropped after the test."""
    pool = ConnectionPool(
        test_db_url,
        open=True,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(mig_schema)
            )
        )
    yield pool, mig_schema
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(mig_schema)
            )
        )
    pool.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_migrate_creates_all_tables(fresh_copy_pool) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate

    migrate(pool, schema)

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
            (schema,),
        ).fetchall()

    names = {r["table_name"] for r in rows}
    expected = {
        "catalog_mapping", "customers", "orders", "order_items",
        "order_status_history", "pending_actions", "return_requests",
        "refunds", "chat_messages",
    }
    assert expected.issubset(names), f"Missing tables: {expected - names}"


def test_migrate_idempotent(fresh_copy_pool) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate

    migrate(pool, schema)
    migrate(pool, schema)   # second call must not raise


def test_delivery_without_address_raises(fresh_copy_pool) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate

    migrate(pool, schema)

    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.customers (phone) VALUES ('0000000001')"
            ).format(s=sql.Identifier(schema))
        )
        cid = conn.execute(
            sql.SQL("SELECT id FROM {s}.customers WHERE phone = '0000000001'").format(
                s=sql.Identifier(schema)
            )
        ).fetchone()["id"]

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {s}.orders "
                    "(order_number, session_id, customer_id, order_type, "
                    " total_amount, delivery_address) "
                    "VALUES ('ORD-TST-0001', 'sess1', %s, 'delivery', 9.99, NULL)"
                ).format(s=sql.Identifier(schema)),
                (cid,),
            )


def test_takeaway_without_pickup_time_raises(fresh_copy_pool) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate

    migrate(pool, schema)

    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.customers (phone) VALUES ('0000000002')"
            ).format(s=sql.Identifier(schema))
        )
        cid = conn.execute(
            sql.SQL("SELECT id FROM {s}.customers WHERE phone = '0000000002'").format(
                s=sql.Identifier(schema)
            )
        ).fetchone()["id"]

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {s}.orders "
                    "(order_number, session_id, customer_id, order_type, "
                    " total_amount, pickup_time) "
                    "VALUES ('ORD-TST-0002', 'sess2', %s, 'takeaway', 9.99, NULL)"
                ).format(s=sql.Identifier(schema)),
                (cid,),
            )


def test_order_item_quantity_zero_raises(fresh_copy_pool) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate

    migrate(pool, schema)

    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.customers (phone) VALUES ('0000000003')"
            ).format(s=sql.Identifier(schema))
        )
        cid = conn.execute(
            sql.SQL("SELECT id FROM {s}.customers WHERE phone = '0000000003'").format(
                s=sql.Identifier(schema)
            )
        ).fetchone()["id"]

        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.orders "
                "(order_number, session_id, customer_id, order_type, "
                " total_amount, delivery_address) "
                "VALUES ('ORD-TST-0003', 'sess3', %s, 'delivery', 9.99, '1 Main St')"
            ).format(s=sql.Identifier(schema)),
            (cid,),
        )
        oid = conn.execute(
            sql.SQL(
                "SELECT id FROM {s}.orders WHERE order_number = 'ORD-TST-0003'"
            ).format(s=sql.Identifier(schema))
        ).fetchone()["id"]

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {s}.order_items "
                    "(order_id, product_id, product_name, unit_price, quantity, line_total) "
                    "VALUES (%s, 1, 'Widget', 9.99, 0, 0.00)"
                ).format(s=sql.Identifier(schema)),
                (oid,),
            )


def test_invalid_order_status_raises(fresh_copy_pool) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate

    migrate(pool, schema)

    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.customers (phone) VALUES ('0000000004')"
            ).format(s=sql.Identifier(schema))
        )
        cid = conn.execute(
            sql.SQL("SELECT id FROM {s}.customers WHERE phone = '0000000004'").format(
                s=sql.Identifier(schema)
            )
        ).fetchone()["id"]

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {s}.orders "
                    "(order_number, session_id, customer_id, order_type, status, "
                    " total_amount, delivery_address) "
                    "VALUES ('ORD-TST-0004', 'sess4', %s, 'delivery', "
                    "        'flying', 9.99, '1 Main St')"
                ).format(s=sql.Identifier(schema)),
                (cid,),
            )


def test_record_tables_applied_returns_true_after_migrate(fresh_copy_pool, monkeypatch) -> None:
    pool, schema = fresh_copy_pool
    from db.migrations import migrate, record_tables_applied
    import config

    monkeypatch.setattr(
        config,
        "load_settings",
        lambda **_kw: type("S", (), {"copy_schema": schema, "require_source": False})(),
    )

    assert not record_tables_applied(pool)
    migrate(pool, schema)
    assert record_tables_applied(pool)
