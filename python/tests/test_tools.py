"""Tool-layer tests — no LLM calls, DB fixtures used where available.

Non-DB tests run always; DB tests are skipped without TEST_DATABASE_URL.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Pydantic input model validation — no DB, no LLM
# ---------------------------------------------------------------------------

def test_prepare_order_delivery_requires_address() -> None:
    from agent.order_agent import PrepareOrderInput
    with pytest.raises(ValidationError, match="delivery_address"):
        PrepareOrderInput(
            phone="9999999999",
            order_type="delivery",
            items=[{"product_id": 1, "quantity": 1}],
            delivery_address=None,
        )


def test_prepare_order_takeaway_requires_pickup_time() -> None:
    from agent.order_agent import PrepareOrderInput
    with pytest.raises(ValidationError, match="pickup_time"):
        PrepareOrderInput(
            phone="9999999999",
            order_type="takeaway",
            items=[{"product_id": 1, "quantity": 1}],
            pickup_time=None,
        )


def test_prepare_order_takeaway_past_pickup_time_raises() -> None:
    from agent.order_agent import PrepareOrderInput
    with pytest.raises(ValidationError, match="future"):
        PrepareOrderInput(
            phone="9999999999",
            order_type="takeaway",
            items=[{"product_id": 1, "quantity": 1}],
            pickup_time="2000-01-01T00:00:00",
        )


def test_prepare_order_zero_quantity_raises() -> None:
    from agent.order_agent import PrepareOrderInput, OrderItem
    with pytest.raises(ValidationError, match="greater than 0"):
        PrepareOrderInput(
            phone="9999999999",
            order_type="delivery",
            items=[{"product_id": 1, "quantity": 0}],
            delivery_address="1 Main St",
        )


def test_prepare_order_invalid_phone_raises() -> None:
    from agent.order_agent import PrepareOrderInput
    with pytest.raises(ValidationError, match="phone"):
        PrepareOrderInput(
            phone="abc",
            order_type="delivery",
            items=[{"product_id": 1, "quantity": 1}],
            delivery_address="1 Main St",
        )


def test_prepare_order_valid_delivery() -> None:
    from agent.order_agent import PrepareOrderInput
    model = PrepareOrderInput(
        phone="9999999999",
        order_type="delivery",
        items=[{"product_id": 1, "quantity": 2}],
        delivery_address="42 Example Road",
    )
    assert model.order_type == "delivery"
    assert model.items[0].quantity == 2


# ---------------------------------------------------------------------------
# DB-backed tool tests — require TEST_DATABASE_URL
# ---------------------------------------------------------------------------

SCHEMA = "tools_test"

pytestmark_db = pytest.mark.db


@pytest.fixture(scope="module")
def tools_pool(test_db_url):
    import psycopg
    from psycopg_pool import ConnectionPool
    from psycopg import sql

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
                "INSERT INTO {s}.products (name, price, stock) VALUES "
                "('Tool Widget', 15.00, 3), ('Empty Item', 5.00, 0)"
            ).format(s=sql.Identifier(SCHEMA))
        )

    yield pool

    with pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA))
        )
    pool.close()


@pytest.fixture(scope="module")
def tools_ctx(tools_pool):
    import os
    from config import Settings
    from db.catalog import CatalogSQL
    from agent.context import AgentContext

    cat = CatalogSQL(
        copy_schema=SCHEMA,
        products_table="products",
        id_col="id",
        name_col="name",
        price_col="price",
        stock_col="stock",
    )
    settings = Settings(
        source_database_url="postgres://x:x@localhost/x",
        copy_database_url=None,
        source_schema="public",
        copy_schema=SCHEMA,
        groq_api_key=None,
        groq_model="llama-3.3-70b-versatile",
        store_name="Test Store",
        currency="INR",
        return_window_days=7,
        confirmation_ttl_minutes=15,
    )
    return AgentContext(
        copy_pool=tools_pool,
        catalog_sql=cat,
        session_id="tools_test_session",
        settings=settings,
    )


@pytest.mark.db
def test_check_availability_returns_false_for_unknown(tools_ctx) -> None:
    from agent.stock_agent import _check_availability_impl
    result = _check_availability_impl(product_id=99999, _ctx=tools_ctx)
    assert result["found"] is False


@pytest.mark.db
def test_search_products_returns_empty_for_no_match(tools_ctx) -> None:
    from agent.stock_agent import _search_products_impl
    result = _search_products_impl(query="xyzzy_absolutely_no_match", _ctx=tools_ctx)
    assert result == []


@pytest.mark.db
def test_check_availability_out_of_stock(tools_ctx, tools_pool) -> None:
    from psycopg import sql
    from agent.stock_agent import _check_availability_impl

    with tools_pool.connection() as conn:
        row = conn.execute(
            sql.SQL("SELECT id FROM {s}.products WHERE name = 'Empty Item'").format(
                s=sql.Identifier(SCHEMA)
            )
        ).fetchone()

    result = _check_availability_impl(product_id=row["id"], _ctx=tools_ctx)
    assert result["found"] is True
    assert result["in_stock"] is False


@pytest.mark.db
def test_prepare_order_insufficient_stock_returns_error(tools_ctx, tools_pool) -> None:
    from psycopg import sql
    from agent.order_agent import _prepare_order_impl

    with tools_pool.connection() as conn:
        row = conn.execute(
            sql.SQL("SELECT id FROM {s}.products WHERE name = 'Empty Item'").format(
                s=sql.Identifier(SCHEMA)
            )
        ).fetchone()

    result = _prepare_order_impl(
        phone="9000000001",
        order_type="delivery",
        items=[{"product_id": row["id"], "quantity": 1}],
        delivery_address="1 Test St",
        _ctx=tools_ctx,
    )
    assert result.get("error") is True
    assert "stock_issues" in result


@pytest.mark.db
def test_get_order_status_wrong_phone_returns_none(tools_ctx) -> None:
    from agent.order_agent import _get_order_status_impl
    result = _get_order_status_impl(
        order_number="ORD-00000000-FAKE",
        phone="0000000000",
        _ctx=tools_ctx,
    )
    assert result is None


@pytest.mark.db
def test_make_review_tools_empty_when_no_reviews_table(tools_ctx) -> None:
    from agent.review_agent import make_review_tools
    tools = make_review_tools(tools_ctx)
    assert tools == []


@pytest.mark.db
def test_make_stock_tools_returns_three_tools(tools_ctx) -> None:
    from agent.stock_agent import make_stock_tools
    tools = make_stock_tools(tools_ctx)
    assert len(tools) == 3
    names = {t.name for t in tools}
    assert names == {"search_products", "check_availability", "find_alternatives"}


@pytest.mark.db
def test_make_order_tools_returns_five_tools(tools_ctx) -> None:
    from agent.order_agent import make_order_tools
    tools = make_order_tools(tools_ctx)
    assert len(tools) == 5
    names = {t.name for t in tools}
    assert names == {
        "prepare_order", "prepare_cancel", "get_order_status",
        "request_return", "get_return_status",
    }


@pytest.mark.db
def test_ctx_never_in_tool_schema(tools_ctx) -> None:
    """_ctx must not appear in any tool's JSON schema."""
    from agent.stock_agent import make_stock_tools
    from agent.order_agent import make_order_tools

    all_tools = make_stock_tools(tools_ctx) + make_order_tools(tools_ctx)
    for tool in all_tools:
        schema = tool.args_schema.model_json_schema() if tool.args_schema else {}
        props = schema.get("properties", {})
        assert "_ctx" not in props, (
            f"Tool {tool.name!r} exposes _ctx in its JSON schema"
        )
