"""Terminal chat loop.

The CLI owns all y/n confirmation prompts — the model never confirms.
session_id is generated here and injected into AgentContext.
"""
from __future__ import annotations

import time
import uuid

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

console = Console()

MAX_INPUT_CHARS = 2000
HISTORY_WINDOW = 20
_RATE_LIMIT_RETRY_WAIT = 3


def chat() -> None:
    """Start the terminal chat agent."""
    import config
    import db.pools as pools
    from db.catalog import catalog_from_mapping
    from db.scanner import load_mapping
    from agent.context import AgentContext
    from agent.main_agent import build_agent, run_turn

    settings = config.load_settings(require_groq=True, require_source=False)

    try:
        mapping = load_mapping(require_confirmed=True)
    except Exception as exc:
        console.print(f"[red]Setup incomplete: {exc}[/red]")
        console.print(
            "Run [bold]python main.py scan[/bold] → "
            "[bold]python main.py confirm-mapping[/bold] → "
            "[bold]python main.py clone[/bold] → "
            "[bold]python main.py migrate[/bold] first."
        )
        raise typer.Exit(1)

    copy_pool = pools.get_copy_pool()
    catalog_sql = catalog_from_mapping(mapping, settings.copy_schema)
    session_id = str(uuid.uuid4())

    ctx = AgentContext(
        copy_pool=copy_pool,
        catalog_sql=catalog_sql,
        session_id=session_id,
        settings=settings,
    )

    try:
        graph = build_agent(ctx)
    except Exception as exc:
        console.print(f"[red]Failed to build agent: {exc.__class__.__name__}: {exc}[/red]")
        raise typer.Exit(1)

    console.print(
        Panel(
            f"[bold cyan]{settings.store_name}[/bold cyan] — AI shopping assistant\n"
            "Commands: [bold]/quit[/bold]  [bold]/new[/bold] (new session)  "
            "[bold]/help[/bold]",
            border_style="cyan",
        )
    )

    messages: list[BaseMessage] = []

    while True:
        try:
            raw = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not raw:
            continue

        if raw.lower() == "/quit":
            console.print("[dim]Goodbye.[/dim]")
            break

        if raw.lower() == "/new":
            messages = []
            session_id = str(uuid.uuid4())
            ctx = AgentContext(
                copy_pool=copy_pool,
                catalog_sql=catalog_sql,
                session_id=session_id,
                settings=settings,
            )
            graph = build_agent(ctx)
            console.print("[dim]New session started.[/dim]")
            continue

        if raw.lower() == "/help":
            console.print(
                "  [bold]/quit[/bold]   — exit\n"
                "  [bold]/new[/bold]    — start a fresh session\n"
                "  [bold]/help[/bold]   — show this message\n\n"
                "Ask about products, place orders, check order status, or request returns."
            )
            continue

        if len(raw) > MAX_INPUT_CHARS:
            console.print(
                f"[yellow]Input too long ({len(raw)} chars). "
                f"Please keep messages under {MAX_INPUT_CHARS} characters.[/yellow]"
            )
            continue

        messages.append(HumanMessage(content=raw))

        reply, pending_action = _invoke_with_retry(graph, messages)

        console.print(f"\n[bold cyan]Assistant:[/bold cyan] {reply}")

        if pending_action:
            _handle_confirmation(pending_action, ctx, copy_pool, settings)

        # Trim history to last HISTORY_WINDOW messages
        if len(messages) > HISTORY_WINDOW:
            messages = messages[-HISTORY_WINDOW:]

        # Append assistant reply to history
        messages.append(AIMessage(content=reply))


def _invoke_with_retry(graph, messages: list[BaseMessage]) -> tuple[str, dict | None]:
    """Invoke the agent, retrying once on rate-limit or transient API errors."""
    from agent.main_agent import run_turn

    try:
        return run_turn(graph, messages)
    except Exception as exc:
        cls_name = exc.__class__.__name__
        if "RateLimitError" in cls_name or "APIError" in cls_name:
            console.print(
                f"[yellow]API issue ({cls_name}). Retrying in {_RATE_LIMIT_RETRY_WAIT}s…[/yellow]"
            )
            time.sleep(_RATE_LIMIT_RETRY_WAIT)
            try:
                return run_turn(graph, messages)
            except Exception as exc2:
                console.print(
                    f"[red]Could not reach the AI service: {exc2.__class__.__name__}. "
                    "Please try again in a moment.[/red]"
                )
                return "", None
        raise


def _handle_confirmation(
    pending_action: dict,
    ctx: AgentContext,
    copy_pool,
    settings,
) -> None:
    """Print the pending-action summary and ask for CLI y/n confirmation."""
    from db.orders_repo import confirm_pending_action, OrderError

    token_str: str = pending_action.get("token", "")
    summary: dict = pending_action.get("summary", {})

    console.print(
        Panel(
            _format_summary(summary),
            title="[bold yellow]Confirm action[/bold yellow]",
            border_style="yellow",
        )
    )

    try:
        answer = input("Confirm? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer == "y":
        try:
            token = uuid.UUID(token_str)
            with copy_pool.connection() as conn:
                result = confirm_pending_action(
                    conn,
                    settings.copy_schema,
                    token,
                    ctx.session_id,
                    ctx.catalog_sql,
                )
            action_type = pending_action.get("summary", {})
            order_num = result.get("order_number", "")
            status = result.get("status", "")
            console.print(
                f"[green]Done.[/green] "
                + (f"Order [bold]{order_num}[/bold] placed — status: {status}" if order_num else f"Status: {status}")
            )
        except OrderError as exc:
            console.print(f"[red]Could not complete action: {exc}[/red]")
        except Exception as exc:
            console.print(f"[red]Unexpected error: {exc.__class__.__name__}: {exc}[/red]")
    else:
        console.print("[dim]Cancelled.[/dim]")


def _format_summary(summary: dict) -> str:
    if not summary:
        return "(no details)"
    lines = []
    for key, val in summary.items():
        if val is None:
            continue
        if key == "items" and isinstance(val, list):
            lines.append("items:")
            for item in val:
                lines.append(
                    f"  • product_id={item.get('product_id')} "
                    f"× {item.get('quantity')}"
                )
        else:
            lines.append(f"{key}: {val}")
    return "\n".join(lines) if lines else "(no details)"
