"""Shared pytest fixtures and markers."""
from __future__ import annotations

import os

import pytest
from psycopg_pool import ConnectionPool
import psycopg

DB_MARKER = "db"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{DB_MARKER}: requires TEST_DATABASE_URL (skipped when not set)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("TEST_DATABASE_URL", "").strip():
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if item.get_closest_marker(DB_MARKER):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def src_schema() -> str:
    return os.environ.get("SOURCE_SCHEMA", "public")


@pytest.fixture(scope="session")
def cpy_schema() -> str:
    val = os.environ.get("COPY_SCHEMA", "agent_copy")
    return val


@pytest.fixture(scope="session")
def copy_pool(test_db_url: str) -> ConnectionPool:
    pool = ConnectionPool(
        test_db_url,
        open=True,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    yield pool
    pool.close()
