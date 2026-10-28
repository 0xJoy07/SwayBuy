"""Cloner integration tests — require TEST_DATABASE_URL (@pytest.mark.db)."""
from __future__ import annotations

import threading
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def src_schema_name() -> str:
    return "clone_test_src"


@pytest.fixture()
def cpy_schema_name() -> str:
    return "clone_test_cpy"


@pytest.fixture()
def fresh_schemas(test_db_url: str, src_schema_name: str, cpy_schema_name: str):
    """Create isolated source + copy schemas for one test, drop after."""
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
                sql.Identifier(src_schema_name)
            )
        )
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(cpy_schema_name)
            )
        )
        conn.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(src_schema_name))
        )
        conn.execute(
            sql.SQL(
                "CREATE TABLE {s}.products ("
                "  id    serial PRIMARY KEY, "
                "  name  text   NOT NULL, "
                "  price numeric(12,2) NOT NULL, "
                "  stock integer NOT NULL DEFAULT 0"
                ")"
            ).format(s=sql.Identifier(src_schema_name))
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.products (name, price, stock) VALUES "
                "('Alpha', 10.00, 5), ('Beta', 20.00, 3), ('Gamma', 30.00, 1)"
            ).format(s=sql.Identifier(src_schema_name))
        )

    yield pool, src_schema_name, cpy_schema_name

    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(src_schema_name)
            )
        )
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(cpy_schema_name)
            )
        )
    pool.close()


def _make_settings(src_schema: str, cpy_schema: str, same_db: bool = True):
    class _S:
        source_schema = src_schema
        copy_schema = cpy_schema
        same_database = same_db
    return _S()


def _make_mapping(table: str = "products") -> dict:
    return {
        "products": {
            "table": table,
            "id_col": "id",
            "name_col": "name",
            "price_col": "price",
            "stock_col": "stock",
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_clone_row_counts_match(fresh_schemas) -> None:
    pool, src, cpy = fresh_schemas
    from db.cloner import clone

    settings = _make_settings(src, cpy)
    mapping = _make_mapping()

    clone(pool, pool, settings, mapping)

    with pool.connection() as conn:
        src_n = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.products").format(
                sql.Identifier(src)
            )
        ).fetchone()["n"]
        cpy_n = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.products").format(
                sql.Identifier(cpy)
            )
        ).fetchone()["n"]

    assert src_n == cpy_n == 3


def test_clone_source_row_count_unchanged_after_order(fresh_schemas) -> None:
    """Source row count must be the same before and after the whole scenario."""
    pool, src, cpy = fresh_schemas
    from db.cloner import clone

    settings = _make_settings(src, cpy)
    mapping = _make_mapping()

    with pool.connection() as conn:
        before = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.products").format(
                sql.Identifier(src)
            )
        ).fetchone()["n"]

    clone(pool, pool, settings, mapping)

    # Simulate order: deduct stock in copy, not source
    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {}.products SET stock = stock - 1 WHERE id = 1"
            ).format(sql.Identifier(cpy))
        )

    # Source count must be unchanged
    with pool.connection() as conn:
        after = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.products").format(
                sql.Identifier(src)
            )
        ).fetchone()["n"]

    assert before == after


def test_clone_sequence_not_in_source_schema(fresh_schemas) -> None:
    """The copy column default must reference a sequence in the copy schema, not the source."""
    pool, src, cpy = fresh_schemas
    from db.cloner import clone

    settings = _make_settings(src, cpy)
    clone(pool, pool, settings, _make_mapping())

    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT pg_get_expr(ad.adbin, ad.adrelid) AS def_expr
            FROM pg_attribute      a
            JOIN pg_attrdef        ad ON ad.adrelid = a.attrelid
                                     AND ad.adnum   = a.attnum
            JOIN pg_class          c  ON c.oid      = a.attrelid
            JOIN pg_namespace      ns ON ns.oid      = c.relnamespace
            WHERE ns.nspname = %s
              AND c.relname  = 'products'
              AND a.attname  = 'id'
            """,
            (cpy,),
        ).fetchone()

    assert row is not None, "No column default found for copy.products.id"
    def_expr: str = row["def_expr"]
    assert src not in def_expr, (
        f"Copy column default still references source schema {src!r}: {def_expr!r}"
    )
    assert cpy in def_expr, (
        f"Copy column default does not reference copy schema {cpy!r}: {def_expr!r}"
    )


def test_clone_fk_enforcement(fresh_schemas) -> None:
    """After clone, FK constraints in the copy schema must be enforced."""
    pool, src, cpy = fresh_schemas
    from db.cloner import clone

    # Add an order_items table that FKs into products
    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "CREATE TABLE {s}.order_items ("
                "  id         serial PRIMARY KEY, "
                "  product_id integer NOT NULL REFERENCES {s}.products(id), "
                "  qty        integer NOT NULL"
                ")"
            ).format(s=sql.Identifier(src))
        )

    settings = _make_settings(src, cpy)
    clone(pool, pool, settings, _make_mapping())

    with pool.connection() as conn:
        # Valid insert — product id=1 exists
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.order_items (product_id, qty) VALUES (1, 2)"
            ).format(sql.Identifier(cpy))
        )
        # FK violation — product id=9999 does not exist
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.order_items (product_id, qty) VALUES (9999, 1)"
                ).format(sql.Identifier(cpy))
            )


def test_clone_aborts_if_copy_has_tables(fresh_schemas, capsys) -> None:
    """Second clone attempt without reset-copy must print an error and return."""
    pool, src, cpy = fresh_schemas
    from db.cloner import clone

    settings = _make_settings(src, cpy)
    clone(pool, pool, settings, _make_mapping())

    # Second clone — should abort, not raise
    from io import StringIO
    from rich.console import Console as RichConsole
    buf = StringIO()
    import db.cloner as cloner_mod
    old_console = cloner_mod.console
    cloner_mod.console = RichConsole(file=buf, highlight=False)
    try:
        clone(pool, pool, settings, _make_mapping())
    finally:
        cloner_mod.console = old_console

    output = buf.getvalue()
    assert "reset-copy" in output.lower() or "already has" in output.lower()
