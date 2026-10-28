"""Review summary tool — only registered when reviews mapping exists."""
from __future__ import annotations

import functools
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.context import AgentContext


class GetReviewSummaryInput(BaseModel):
    product_id: int = Field(description="Numeric product ID to get reviews for.")


def _get_review_summary_impl(
    product_id: int,
    *,
    _ctx: AgentContext,
) -> dict[str, Any]:
    with _ctx.copy_pool.connection() as conn:
        result = _ctx.catalog_sql.get_review_summary(conn, product_id)
    if result is None:
        return {"found": False, "message": "Review data is not available for this store."}
    return result


def make_review_tools(ctx: AgentContext) -> list[BaseTool]:
    """Return [get_review_summary] only when a reviews table is mapped."""
    if ctx.catalog_sql.reviews_table is None:
        return []

    @functools.wraps(_get_review_summary_impl)
    def get_review_summary(product_id: int) -> dict[str, Any]:
        return _get_review_summary_impl(product_id, _ctx=ctx)

    return [
        StructuredTool.from_function(
            func=get_review_summary,
            name="get_review_summary",
            description=(
                "Get average rating and recent review snippets for a product. "
                "Returns review_count=0 when no reviews exist. "
                "Only use data returned by this tool — never invent ratings."
            ),
            args_schema=GetReviewSummaryInput,
        )
    ]
