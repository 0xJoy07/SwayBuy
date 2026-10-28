"""Agent smoke test — skipped unless both TEST_DATABASE_URL and GROQ_API_KEY are set.

One full round-trip: "do you have any products?" must return a non-empty string
without raising.  No order is placed.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def smoke_ctx(test_db_url: str):
    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool
    from config import Settings
    from db.catalog import CatalogSQL
    from db.migrations import migrate
    from agent.context import AgentContext

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_key:
        pytest.skip("GROQ_API_KEY not set")

    schema = "smoke_test"
    pool = ConnectionPool(
        test_db_url,
        open=True,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": psycopg.rows.dict_row},
    )
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
    migrate(pool, schema)

    with pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {s}.products ("
                "  id    serial PRIMARY KEY, "
                "  name  text NOT NULL, "
                "  price numeric(12,2) NOT NULL, "
                "  stock integer NOT NULL DEFAULT 0"
                ")"
            ).format(s=sql.Identifier(schema))
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.products (name, price, stock) "
                "VALUES ('Smoke Widget', 9.99, 10)"
            ).format(s=sql.Identifier(schema))
        )

    settings = Settings(
        source_database_url="postgres://x:x@localhost/x",
        copy_database_url=None,
        source_schema="public",
        copy_schema=schema,
        groq_api_key=groq_key,
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        store_name="Smoke Test Store",
        currency="INR",
        return_window_days=7,
        confirmation_ttl_minutes=15,
    )
    cat = CatalogSQL(
        copy_schema=schema,
        products_table="products",
        id_col="id",
        name_col="name",
        price_col="price",
        stock_col="stock",
    )
    ctx = AgentContext(
        copy_pool=pool,
        catalog_sql=cat,
        session_id="smoke_session_001",
        settings=settings,
    )
    yield ctx

    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
    pool.close()


def test_agent_smoke_one_turn(smoke_ctx) -> None:
    from langchain_core.messages import HumanMessage
    from agent.main_agent import build_agent, run_turn

    graph = build_agent(smoke_ctx)
    messages = [HumanMessage(content="Do you have any products available?")]
    reply, pending_action = run_turn(graph, messages)

    assert isinstance(reply, str), "reply must be a string"
    assert len(reply) > 0, "reply must not be empty"
    assert pending_action is None, "no confirmation expected for a product query"
