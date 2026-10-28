"""LangChain agent wiring.

build_agent() creates the CompiledStateGraph once per session.
run_turn()    invokes it and extracts the reply + any pending-action token.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from agent.context import AgentContext
from agent.order_agent import make_order_tools
from agent.prompts import build_system_prompt
from agent.review_agent import make_review_tools
from agent.stock_agent import make_stock_tools


def build_agent(ctx: AgentContext):
    """Return a CompiledStateGraph wired with all tools for this session."""
    llm = ChatGroq(
        model=ctx.settings.groq_model,
        temperature=0.1,
        api_key=ctx.settings.groq_api_key,
        max_retries=2,
    )

    tools = (
        make_stock_tools(ctx)
        + make_review_tools(ctx)
        + make_order_tools(ctx)
    )

    has_reviews = ctx.catalog_sql.reviews_table is not None
    system_prompt = build_system_prompt(
        store_name=ctx.settings.store_name,
        currency=ctx.settings.currency,
        return_window_days=ctx.settings.return_window_days,
        has_reviews=has_reviews,
    )

    graph = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )
    return graph


def run_turn(
    graph,
    messages: list[BaseMessage],
) -> tuple[str, dict[str, Any] | None]:
    """Invoke the agent graph for one turn.

    Returns:
        reply:          The last AIMessage content string.
        pending_action: A dict with token + summary if a confirmation is needed,
                        otherwise None.
    """
    result = graph.invoke({"messages": messages})
    result_messages: list[BaseMessage] = result.get("messages", [])

    # Extract the last AIMessage
    reply = ""
    for msg in reversed(result_messages):
        if isinstance(msg, AIMessage):
            reply = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    # Scan tool messages for a pending_action token
    pending_action: dict[str, Any] | None = None
    for msg in result_messages:
        content = msg.content if isinstance(msg.content, str) else ""
        if not content:
            continue
        # Tool results are plain text/JSON; look for requires_confirmation
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("requires_confirmation"):
                pending_action = data
                break
        except (json.JSONDecodeError, ValueError):
            pass

    return reply, pending_action
