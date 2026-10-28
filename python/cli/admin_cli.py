"""Admin sub-commands: orders, order, set-status, returns, return-action,
refund-create, refund-set.

All commands:
- load settings with require_groq=False, require_source=False (copy pool only)
- never import the source pool
- use rich for output
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

admin_app = typer.Typer(
    name="admin",
    help="Admin commands for inspecting and managing orders/returns/refunds.",
    no_args_is_help=True,
)
console = Console()


def _get_pool_and_settings():
    import config
    import db.pools as pools

    settings = config.load_settings(require_groq=False, require_source=False)
    pool = pools.get_copy_pool()
    return pool, settings


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

@admin_app.command("orders")
def orders_command(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by order status."),
) -> None:
    """List orders, optionally filtered by status."""
    from psycopg import sql

    pool, settings = _get_pool_and_settings()
    s = sql.Identifier(settings.copy_schema)

    with pool.connection() as conn:
        if status:
            rows = conn.execute(
                sql.SQL(
                    "SELECT o.order_number, o.status, o.order_type, "
                    "       o.total_amount, o.created_at, c.phone "
                    "FROM {s}.orders o "
                    "JOIN {s}.customers c ON c.id = o.customer_id "
                    "WHERE o.status = %s "
                    "ORDER BY o.created_at DESC LIMIT 100"
                ).format(s=s),
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                sql.SQL(
                    "SELECT o.order_number, o.status, o.order_type, "
                    "       o.total_amount, o.created_at, c.phone "
                    "FROM {s}.orders o "
                    "JOIN {s}.customers c ON c.id = o.customer_id "
                    "ORDER BY o.created_at DESC LIMIT 100"
                ).format(s=s),
            ).fetchall()

    if not rows:
        console.print("[dim]No orders found.[/dim]")
        return

    t = Table(title="Orders", show_lines=True)
    t.add_column("Order #", style="cyan")
    t.add_column("Status")
    t.add_column("Type")
    t.add_column("Total", justify="right")
    t.add_column("Phone")
    t.add_column("Created")

    for r in rows:
        t.add_row(
            r["order_number"],
            r["status"],
            r["order_type"],
            str(r["total_amount"]),
            r["phone"],
            r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "",
        )
    console.print(t)


# ---------------------------------------------------------------------------
# order (single order detail)
# ---------------------------------------------------------------------------

@admin_app.command("order")
def order_command(
    order_number: str = typer.Argument(..., help="Order number to inspect."),
) -> None:
    """Show full details of a single order including items and status history."""
    from psycopg import sql

    pool, settings = _get_pool_and_settings()
    s = sql.Identifier(settings.copy_schema)

    with pool.connection() as conn:
        order = conn.execute(
            sql.SQL(
                "SELECT o.id, o.order_number, o.status, o.payment_status, "
                "       o.order_type, o.total_amount, o.delivery_address, "
                "       o.pickup_time, o.notes, o.created_at, c.phone "
                "FROM {s}.orders o "
                "JOIN {s}.customers c ON c.id = o.customer_id "
                "WHERE o.order_number = %s"
            ).format(s=s),
            (order_number,),
        ).fetchone()

        if order is None:
            console.print(f"[red]Order {order_number!r} not found.[/red]")
            raise typer.Exit(1)

        items = conn.execute(
            sql.SQL(
                "SELECT product_name, quantity, unit_price, line_total "
                "FROM {s}.order_items WHERE order_id = %s ORDER BY id"
            ).format(s=s),
            (order["id"],),
        ).fetchall()

        history = conn.execute(
            sql.SQL(
                "SELECT old_status, new_status, note, changed_by, changed_at "
                "FROM {s}.order_status_history "
                "WHERE order_id = %s ORDER BY changed_at"
            ).format(s=s),
            (order["id"],),
        ).fetchall()

    console.print(f"\n[bold cyan]Order {order['order_number']}[/bold cyan]")
    console.print(f"  Status:    {order['status']}  |  Payment: {order['payment_status']}")
    console.print(f"  Type:      {order['order_type']}")
    if order["delivery_address"]:
        console.print(f"  Address:   {order['delivery_address']}")
    if order["pickup_time"]:
        console.print(f"  Pickup:    {order['pickup_time']}")
    console.print(f"  Total:     {order['total_amount']}")
    console.print(f"  Customer:  {order['phone']}")
    console.print(f"  Created:   {order['created_at']}")

    it = Table(title="Items", show_lines=True)
    it.add_column("Product")
    it.add_column("Qty", justify="right")
    it.add_column("Unit price", justify="right")
    it.add_column("Line total", justify="right")
    for i in items:
        it.add_row(
            i["product_name"],
            str(i["quantity"]),
            str(i["unit_price"]),
            str(i["line_total"]),
        )
    console.print(it)

    ht = Table(title="Status history", show_lines=True)
    ht.add_column("From")
    ht.add_column("To", style="cyan")
    ht.add_column("Note")
    ht.add_column("By")
    ht.add_column("At")
    for h in history:
        ht.add_row(
            h["old_status"] or "—",
            h["new_status"],
            h["note"] or "",
            h["changed_by"],
            h["changed_at"].strftime("%Y-%m-%d %H:%M") if h["changed_at"] else "",
        )
    console.print(ht)


# ---------------------------------------------------------------------------
# set-status
# ---------------------------------------------------------------------------

@admin_app.command("set-status")
def set_status_command(
    order_number: str = typer.Argument(..., help="Order number to update."),
    status: str = typer.Argument(..., help="New status."),
    note: str = typer.Option("", "--note", help="Optional note for the history entry."),
) -> None:
    """Transition an order to a new status (admin-side)."""
    from db.orders_repo import transition_order_status, OrderError

    pool, settings = _get_pool_and_settings()

    try:
        with pool.connection() as conn:
            transition_order_status(
                conn,
                settings.copy_schema,
                order_number,
                status,
                note or f"Admin set status to {status!r}",
                changed_by="admin",
            )
        console.print(
            f"[green]Order {order_number!r} transitioned to {status!r}.[/green]"
        )
    except OrderError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------

@admin_app.command("returns")
def returns_command(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by return status."),
) -> None:
    """List return requests, optionally filtered by status."""
    from psycopg import sql

    pool, settings = _get_pool_and_settings()
    s = sql.Identifier(settings.copy_schema)

    with pool.connection() as conn:
        if status:
            rows = conn.execute(
                sql.SQL(
                    "SELECT rr.id, o.order_number, rr.quantity, rr.reason, "
                    "       rr.status, rr.requested_at, c.phone "
                    "FROM {s}.return_requests rr "
                    "JOIN {s}.orders o ON o.id = rr.order_id "
                    "JOIN {s}.customers c ON c.id = rr.customer_id "
                    "WHERE rr.status = %s "
                    "ORDER BY rr.requested_at DESC LIMIT 100"
                ).format(s=s),
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                sql.SQL(
                    "SELECT rr.id, o.order_number, rr.quantity, rr.reason, "
                    "       rr.status, rr.requested_at, c.phone "
                    "FROM {s}.return_requests rr "
                    "JOIN {s}.orders o ON o.id = rr.order_id "
                    "JOIN {s}.customers c ON c.id = rr.customer_id "
                    "ORDER BY rr.requested_at DESC LIMIT 100"
                ).format(s=s),
            ).fetchall()

    if not rows:
        console.print("[dim]No return requests found.[/dim]")
        return

    t = Table(title="Return requests", show_lines=True)
    t.add_column("ID", justify="right")
    t.add_column("Order #", style="cyan")
    t.add_column("Qty", justify="right")
    t.add_column("Status")
    t.add_column("Phone")
    t.add_column("Reason")
    t.add_column("Requested")

    for r in rows:
        t.add_row(
            str(r["id"]),
            r["order_number"],
            str(r["quantity"]),
            r["status"],
            r["phone"],
            (r["reason"] or "")[:40],
            r["requested_at"].strftime("%Y-%m-%d %H:%M") if r["requested_at"] else "",
        )
    console.print(t)


# ---------------------------------------------------------------------------
# return-action
# ---------------------------------------------------------------------------

@admin_app.command("return-action")
def return_action_command(
    return_id: int = typer.Argument(..., help="Return request ID."),
    action: str = typer.Argument(
        ...,
        help="Action: approve | reject | mark_received | close",
    ),
    restock: bool = typer.Option(
        False, "--restock",
        help="Restore stock when marking item as received.",
    ),
) -> None:
    """Transition a return request to a new status."""
    from db.returns_repo import transition_return, ReturnError
    from db.catalog import CatalogSQL
    from db.scanner import load_mapping

    _ACTION_TO_STATUS = {
        "approve":       "approved",
        "reject":        "rejected",
        "mark_received": "item_received",
        "close":         "closed",
    }
    new_status = _ACTION_TO_STATUS.get(action)
    if new_status is None:
        console.print(
            f"[red]Unknown action {action!r}. "
            "Choose: approve | reject | mark_received | close[/red]"
        )
        raise typer.Exit(1)

    pool, settings = _get_pool_and_settings()

    try:
        mapping = load_mapping(require_confirmed=True)
        catalog_sql = _catalog_from_settings(mapping, settings)
    except Exception as exc:
        console.print(f"[yellow]Warning: could not load mapping ({exc}). "
                      "Restock will be skipped.[/yellow]")
        catalog_sql = None
        restock = False

    from db.catalog import CatalogSQL as _CatalogSQL
    if catalog_sql is None:
        catalog_sql = _CatalogSQL(
            copy_schema=settings.copy_schema,
            products_table="_noop",
            id_col="id",
            name_col="name",
            price_col="price",
            stock_col="stock",
        )

    try:
        with pool.connection() as conn:
            result = transition_return(
                conn,
                settings.copy_schema,
                return_id,
                new_status,
                catalog_sql,
                restock=restock,
            )
        console.print(
            f"[green]Return {return_id} → {result['status']}.[/green]"
            + (" Stock restored." if restock and new_status == "item_received" else "")
        )
    except ReturnError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# refund-create
# ---------------------------------------------------------------------------

@admin_app.command("refund-create")
def refund_create_command(
    order_number: str = typer.Argument(..., help="Order number to refund."),
    amount: str = typer.Option(..., "--amount", help="Refund amount (e.g. 9.99)."),
    return_id: Optional[int] = typer.Option(
        None, "--return-id", help="Optional linked return request ID."
    ),
    note: str = typer.Option("", "--note", help="Optional note."),
) -> None:
    """Create a refund row for an order."""
    from db.returns_repo import create_refund, ReturnError

    try:
        decimal_amount = Decimal(amount)
    except InvalidOperation:
        console.print(f"[red]Invalid amount: {amount!r}. Must be a decimal number.[/red]")
        raise typer.Exit(1)

    pool, settings = _get_pool_and_settings()

    try:
        with pool.connection() as conn:
            result = create_refund(
                conn,
                settings.copy_schema,
                order_number,
                decimal_amount,
                return_request_id=return_id,
                note=note or None,
            )
        console.print(
            f"[green]Refund {result['refund_id']} created "
            f"(status: {result['status']}).[/green]"
        )
    except ReturnError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# refund-set
# ---------------------------------------------------------------------------

@admin_app.command("refund-set")
def refund_set_command(
    refund_id: int = typer.Argument(..., help="Refund ID to update."),
    status: str = typer.Argument(
        ..., help="New status: pending | processing | completed | failed"
    ),
) -> None:
    """Update the status of a refund row."""
    from db.returns_repo import set_refund_status, ReturnError

    pool, settings = _get_pool_and_settings()

    try:
        with pool.connection() as conn:
            set_refund_status(conn, settings.copy_schema, refund_id, status)
        console.print(
            f"[green]Refund {refund_id} status set to {status!r}.[/green]"
        )
    except ReturnError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _catalog_from_settings(mapping: dict, settings) -> "CatalogSQL":
    from db.catalog import catalog_from_mapping
    return catalog_from_mapping(mapping, settings.copy_schema)
