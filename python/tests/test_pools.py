"""Pool integration tests — require TEST_DATABASE_URL."""
from __future__ import annotations

import pytest
import psycopg
from psycopg_pool import ConnectionPool

pytestmark = pytest.mark.db


def test_source_pool_read_only(test_db_url: str) -> None:
    pool = ConnectionPool(
        test_db_url,
        configure=lambda conn: conn.execute(
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
        ),
        open=True,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    try:
        with pool.connection() as conn:
            row = conn.execute("SHOW transaction_read_only").fetchone()
        assert row["transaction_read_only"] == "on", (
            "Source pool must open sessions in read-only mode"
        )
    finally:
        pool.close()


def test_copy_pool_writable(test_db_url: str, cpy_schema: str) -> None:
    pool = ConnectionPool(
        test_db_url,
        open=True,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    try:
        with pool.connection() as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {cpy_schema}")
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {cpy_schema}._pool_write_test "
                f"(id serial PRIMARY KEY)"
            )
            conn.execute(f"INSERT INTO {cpy_schema}._pool_write_test DEFAULT VALUES")
            n = conn.execute(
                f"SELECT count(*) AS n FROM {cpy_schema}._pool_write_test"
            ).fetchone()["n"]
            assert n >= 1
            conn.execute(f"DROP TABLE {cpy_schema}._pool_write_test")
    finally:
        pool.close()
