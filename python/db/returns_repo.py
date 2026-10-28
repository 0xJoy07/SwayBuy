"""Return requests and refund management.

All writes go to the copy pool. Source pool is never imported here.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql

from db.catalog import CatalogSQL


class ReturnError(Exception):
    """Raised for business-rule violations in the returns/refund flow."""


def _s(schema: str) -> sql.Identifier:
    return sql.Identifier(schema)


# ---------------------------------------------------------------------------
# Return requests
# ---------------------------------------------------------------------------

def request_return(
    conn: psycopg.Connection,
    copy_schema: str,
    order_number: str,
    phone: str,
    order_item_id: int,
    quantity: int,
    reason: str | None,
    return_window_days: int,
) -> dict[str, Any]:
    """Validate and create a return_requests row."""
    # Verify order exists and belongs to this customer
    order = conn.execute(
        sql.SQL(
            "SELECT o.id, o.status, o.created_at, c.id AS customer_id "
            "FROM {s}.orders o "
            "JOIN {s}.customers c ON c.id = o.customer_id "
            "WHERE o.order_number = %s AND c.phone = %s"
        ).format(s=_s(copy_schema)),
        (order_number, phone),
    ).fetchone()
    if order is None:
        raise ReturnError(f"Order {order_number!r} not found for this customer.")
    if order["status"] != "completed":
        raise ReturnError(
            f"Returns are only accepted for completed orders "
            f"(current status: {order['status']!r})."
        )

    created_at = order["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    deadline = created_at + timedelta(days=return_window_days)
    if datetime.now(tz=timezone.utc) > deadline:
        raise ReturnError(
            f"Return window of {return_window_days} day(s) has closed for this order."
        )

    order_id: int = order["id"]
    customer_id: int = order["customer_id"]

    # Verify item belongs to this order
    item_row = conn.execute(
        sql.SQL(
            "SELECT id, quantity FROM {s}.order_items "
            "WHERE id = %s AND order_id = %s"
        ).format(s=_s(copy_schema)),
        (order_item_id, order_id),
    ).fetchone()
    if item_row is None:
        raise ReturnError("Order item not found in this order.")

    purchased_qty: int = item_row["quantity"]

    # Sum already-returned quantity for this item
    already_returned = conn.execute(
        sql.SQL(
            "SELECT COALESCE(sum(quantity), 0) AS n "
            "FROM {s}.return_requests "
            "WHERE order_item_id = %s AND status NOT IN ('rejected')"
        ).format(s=_s(copy_schema)),
        (order_item_id,),
    ).fetchone()["n"]

    max_returnable = purchased_qty - int(already_returned)
    if quantity > max_returnable:
        raise ReturnError(
            f"Cannot return {quantity} unit(s): only {max_returnable} unit(s) "
            "are eligible for return."
        )

    row = conn.execute(
        sql.SQL(
            "INSERT INTO {s}.return_requests "
            "  (order_id, order_item_id, customer_id, quantity, reason) "
            "VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, status"
        ).format(s=_s(copy_schema)),
        (order_id, order_item_id, customer_id, quantity, reason),
    ).fetchone()

    return {"return_id": row["id"], "status": row["status"]}


def get_return_status(
    conn: psycopg.Connection,
    copy_schema: str,
    return_id: int,
    phone: str,
) -> dict[str, Any] | None:
    """Return the return_request row, or None if not found / phone mismatch."""
    row = conn.execute(
        sql.SQL(
            "SELECT rr.id, rr.status, rr.quantity, rr.reason, rr.updated_at "
            "FROM {s}.return_requests rr "
            "JOIN {s}.customers c ON c.id = rr.customer_id "
            "WHERE rr.id = %s AND c.phone = %s"
        ).format(s=_s(copy_schema)),
        (return_id, phone),
    ).fetchone()
    return dict(row) if row else None


_RETURN_TRANSITIONS: dict[str, set[str]] = {
    "requested":     {"approved", "rejected"},
    "approved":      {"item_received"},
    "rejected":      set(),
    "item_received": {"closed"},
    "closed":        set(),
}


def transition_return(
    conn: psycopg.Connection,
    copy_schema: str,
    return_id: int,
    new_status: str,
    catalog_sql: CatalogSQL,
    restock: bool = False,
) -> dict[str, Any]:
    """Transition a return_request to a new status."""
    row = conn.execute(
        sql.SQL(
            "SELECT id, status, order_item_id, quantity "
            "FROM {s}.return_requests WHERE id = %s"
        ).format(s=_s(copy_schema)),
        (return_id,),
    ).fetchone()
    if row is None:
        raise ReturnError(f"Return request {return_id} not found.")

    current: str = row["status"]
    if new_status not in _RETURN_TRANSITIONS.get(current, set()):
        raise ReturnError(
            f"Cannot transition return from {current!r} to {new_status!r}."
        )

    if new_status == "item_received" and restock:
        # Restore stock to the copy catalog
        item = conn.execute(
            sql.SQL(
                "SELECT product_id FROM {s}.order_items WHERE id = %s"
            ).format(s=_s(copy_schema)),
            (row["order_item_id"],),
        ).fetchone()
        if item:
            catalog_sql.restore_stock(conn, item["product_id"], row["quantity"])

    conn.execute(
        sql.SQL(
            "UPDATE {s}.return_requests "
            "SET status = %s, updated_at = now() WHERE id = %s"
        ).format(s=_s(copy_schema)),
        (new_status, return_id),
    )
    return {"return_id": return_id, "status": new_status}


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def create_refund(
    conn: psycopg.Connection,
    copy_schema: str,
    order_number: str,
    amount: Decimal,
    return_request_id: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Create a refund row; validate amount <= unrefunded order value."""
    order = conn.execute(
        sql.SQL(
            "SELECT id, total_amount FROM {s}.orders WHERE order_number = %s"
        ).format(s=_s(copy_schema)),
        (order_number,),
    ).fetchone()
    if order is None:
        raise ReturnError(f"Order {order_number!r} not found.")

    order_id: int = order["id"]
    total_amount = Decimal(str(order["total_amount"]))

    already_refunded = conn.execute(
        sql.SQL(
            "SELECT COALESCE(sum(amount), 0) AS n "
            "FROM {s}.refunds "
            "WHERE order_id = %s AND status NOT IN ('failed')"
        ).format(s=_s(copy_schema)),
        (order_id,),
    ).fetchone()["n"]

    unrefunded = total_amount - Decimal(str(already_refunded))
    if amount > unrefunded:
        raise ReturnError(
            f"Refund amount {amount} exceeds unrefunded order value {unrefunded}."
        )

    row = conn.execute(
        sql.SQL(
            "INSERT INTO {s}.refunds "
            "  (order_id, return_request_id, amount, note) "
            "VALUES (%s, %s, %s, %s) "
            "RETURNING id, status"
        ).format(s=_s(copy_schema)),
        (order_id, return_request_id, amount, note),
    ).fetchone()

    return {"refund_id": row["id"], "status": row["status"]}


def set_refund_status(
    conn: psycopg.Connection,
    copy_schema: str,
    refund_id: int,
    new_status: str,
) -> None:
    """Update refund status; if all refunds for the order are processed,
    set payment_status = refunded on the order."""
    valid_statuses = {"pending", "processing", "completed", "failed"}
    if new_status not in valid_statuses:
        raise ReturnError(f"Invalid refund status: {new_status!r}")

    row = conn.execute(
        sql.SQL(
            "UPDATE {s}.refunds SET status = %s, updated_at = now() "
            "WHERE id = %s RETURNING order_id"
        ).format(s=_s(copy_schema)),
        (new_status, refund_id),
    ).fetchone()
    if row is None:
        raise ReturnError(f"Refund {refund_id} not found.")

    if new_status in ("completed", "failed"):
        order_id: int = row["order_id"]
        pending = conn.execute(
            sql.SQL(
                "SELECT count(*) AS n FROM {s}.refunds "
                "WHERE order_id = %s AND status NOT IN ('completed', 'failed')"
            ).format(s=_s(copy_schema)),
            (order_id,),
        ).fetchone()["n"]
        if int(pending) == 0:
            conn.execute(
                sql.SQL(
                    "UPDATE {s}.orders SET payment_status = 'refunded', "
                    "updated_at = now() WHERE id = %s"
                ).format(s=_s(copy_schema)),
                (order_id,),
            )
