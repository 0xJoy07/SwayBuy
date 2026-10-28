"""Stock / catalog tools — search, availability, alternatives.

Tool factories follow this pattern:
  - Public make_*_tools(ctx) returns list[BaseTool]
  - Each tool's impl function accepts a _ctx keyword arg injected via a
    functools.wraps wrapper — _ctx never appears in the tool's JSON schema.
"""
from __future__ import annotations

import functools
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.context import AgentContext


# ---------------------------------------------------------------------------
# search_products
# ---------------------------------------------------------------------------

class SearchProductsInput(BaseModel):
    query: str = Field(description="Search term to match against product names.")
    limit: int = Field(default=10, description="Maximum number of results to return.")


def _search_products_impl(
    query: str,
    limit: int = 10,
    *,
    _ctx: AgentContext,
) -> list[dict[str, Any]]:
    with _ctx.copy_pool.connection() as conn:
        results = _ctx.catalog_sql.search(conn, query, limit=limit)
    if not results:
        return []
    return [_stringify_decimals(r) for r in results]


# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------

class CheckAvailabilityInput(BaseModel):
    product_id: int = Field(description="Numeric product ID to check.")


def _check_availability_impl(
    product_id: int,
    *,
    _ctx: AgentContext,
) -> dict[str, Any]:
    with _ctx.copy_pool.connection() as conn:
        result = _ctx.catalog_sql.check_availability(conn, product_id)
    if result is None:
        return {"found": False, "product_id": product_id}
    return {"found": True, "in_stock": result["in_stock"], **_stringify_decimals(result)}


# ---------------------------------------------------------------------------
# find_alternatives
# ---------------------------------------------------------------------------

class FindAlternativesInput(BaseModel):
    product_id: int = Field(
        description="Product ID for which to find in-stock alternatives."
    )


def _find_alternatives_impl(
    product_id: int,
    *,
    _ctx: AgentContext,
) -> list[dict[str, Any]]:
    with _ctx.copy_pool.connection() as conn:
        results = _ctx.catalog_sql.find_alternatives(conn, product_id)
    return [_stringify_decimals(r) for r in results]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_stock_tools(ctx: AgentContext) -> list[BaseTool]:

    @functools.wraps(_search_products_impl)
    def search_products(query: str, limit: int = 10) -> list[dict[str, Any]]:
        return _search_products_impl(query, limit, _ctx=ctx)

    @functools.wraps(_check_availability_impl)
    def check_availability(product_id: int) -> dict[str, Any]:
        return _check_availability_impl(product_id, _ctx=ctx)

    @functools.wraps(_find_alternatives_impl)
    def find_alternatives(product_id: int) -> list[dict[str, Any]]:
        return _find_alternatives_impl(product_id, _ctx=ctx)

    return [
        StructuredTool.from_function(
            func=search_products,
            name="search_products",
            description=(
                "Search the product catalog by name. "
                "Returns a list of matching products with id, name, price, and stock. "
                "Returns an empty list when nothing matches — do not invent products."
            ),
            args_schema=SearchProductsInput,
        ),
        StructuredTool.from_function(
            func=check_availability,
            name="check_availability",
            description=(
                "Check whether a specific product is in stock. "
                "Returns found=False when the product ID does not exist. "
                "Never invent availability information."
            ),
            args_schema=CheckAvailabilityInput,
        ),
        StructuredTool.from_function(
            func=find_alternatives,
            name="find_alternatives",
            description=(
                "Find in-stock alternatives for an out-of-stock product. "
                "Returns products in the same category within ±35%% of the original price."
            ),
            args_schema=FindAlternativesInput,
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stringify_decimals(d: dict[str, Any]) -> dict[str, Any]:
    from decimal import Decimal
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in d.items()}
