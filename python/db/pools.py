"""Connection pool management.

Source pool is always opened read-only.
Copy pool is read-write.
Neither pool is imported by the agent, order, return, refund, or admin code
directly — they receive pool objects passed in by the CLI layer.
"""
from __future__ import annotations

import threading
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

import config

_lock = threading.Lock()
_source_pool: ConnectionPool | None = None
_copy_pool: ConnectionPool | None = None


def _set_read_only(conn: psycopg.Connection[Any]) -> None:
    # The configure callback must leave the connection in idle (not INTRANS)
    # state.  Switch to autocommit so the SET runs outside a transaction block,
    # then restore the default autocommit=False for normal pool use.
    conn.autocommit = True
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.autocommit = False


def get_source_pool() -> ConnectionPool:
    global _source_pool
    with _lock:
        if _source_pool is None or _source_pool.closed:
            settings = config.load_settings(require_source=True)
            assert settings.copy_schema != "public", (
                "COPY_SCHEMA must never be 'public'."
            )
            assert settings.copy_schema != settings.source_schema, (
                f"COPY_SCHEMA ({settings.copy_schema!r}) must differ from "
                f"SOURCE_SCHEMA ({settings.source_schema!r})."
            )
            _source_pool = ConnectionPool(
                settings.source_database_url,
                configure=_set_read_only,
                open=True,
                min_size=1,
                max_size=4,
                kwargs={"row_factory": psycopg.rows.dict_row},
            )
    return _source_pool


def get_copy_pool() -> ConnectionPool:
    global _copy_pool
    with _lock:
        if _copy_pool is None or _copy_pool.closed:
            settings = config.load_settings(require_source=False)
            url = settings.copy_database_url or settings.source_database_url
            _copy_pool = ConnectionPool(
                url,
                open=True,
                min_size=1,
                max_size=8,
                kwargs={"row_factory": psycopg.rows.dict_row},
            )
    return _copy_pool


def close_pools() -> None:
    global _source_pool, _copy_pool
    with _lock:
        if _source_pool is not None and not _source_pool.closed:
            _source_pool.close()
            _source_pool = None
        if _copy_pool is not None and not _copy_pool.closed:
            _copy_pool.close()
            _copy_pool = None
