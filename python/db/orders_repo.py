"""Order placement, cancellation, status transitions, and pending-action tokens.

All writes go to the copy pool only. Source pool is never imported here.
All SQL is fixed and parameterized; identifiers via psycopg.sql.Identifier.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from db.catalog import CatalogSQL


# ---------------------------------------------------------------------------
# Valid order-status transitions
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending":          {"confirmed", "cancelled"},
    "confirmed":        {"preparing", "cancelled"},
    "preparing":        {"ready", "cancelled"},
    "ready":            {"out_for_delivery", "completed", "cancelled"},
    "out_for_delivery": {"completed", "cancelled"},
    "completed":        {"refund_pending"},
    "cancelled":        set(),
    "refund_pending":   set(),
}


class OrderError(Exception):
    """Raised for business-rule violations in the order flow."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_order_number() -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:4].upper()
    return f"ORD-{today}-{suffix}"


def _s(schema: str) -> sql.Identifier:
    return sql.Identifier(schema)


# ---------------------------------------------------------------------------
# Pending-action tokens
# ---------------------------------------------------------------------------

def create_pending_action(
    conn: psycopg.Connection,
    copy_schema: str,
    session_id: str,
    action_type: str,
    payload: dict[str, Any],
    ttl_minutes: int,
) -> uuid.UUID:
    """Insert a pending_actions row and return its UUID token."""
    row = conn.execute(
        sql.SQL(
            "INSERT INTO {s}.pending_actions "
            "  (session_id, action_type, payload, expires_at) "
            "VALUES (%s, %s, %s, now() + %s * interval '1 minute') "
            "RETURNING id"
        ).format(s=_s(copy_schema)),
        (session_id, action_type, json.dumps(payload), ttl_minutes),
    ).fetchone()
    return row["id"]


def confirm_pending_action(
    conn: psycopg.Connection,
    copy_schema: str,
    token: uuid.UUID,
    session_id: str,
    catalog_sql: CatalogSQL,
) -> dict[str, Any]:
    """Validate and dispatch a pending action, returning the result dict."""
    row = conn.execute(
        sql.SQL(
            "SELECT id, action_type, payload, expires_at, consumed, session_id "
            "FROM {s}.pending_actions WHERE id = %s"
        ).format(s=_s(copy_schema)),
        (str(token),),
    ).fetchone()

    if row is None:
        raise OrderError("Confirmation token not found.")
    if row["consumed"]:
        raise OrderError("This confirmation token has already been used.")
    if row["session_id"] != session_id:
        raise OrderError("Confirmation token does not belong to this session.")
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(tz=timezone.utc):
        raise OrderError("Confirmation token has expired.")

    action_type: str = row["action_type"]
    payload: dict[str, Any] = row["payload"] if isinstance(row["payload"], dict) \
        else json.loads(row["payload"])

    if action_type == "place_order":
        result = _place_order(conn, copy_schema, payload, catalog_sql)
    elif action_type == "cancel_order":
        result = _cancel_order(conn, copy_schema, payload, catalog_sql)
    else:
        raise OrderError(f"Unknown action_type: {action_type!r}")

    conn.execute(
        sql.SQL(
            "UPDATE {s}.pending_actions SET consumed = true WHERE id = %s"
        ).format(s=_s(copy_schema)),
        (str(token),),
    )
    return result


# ---------------------------------------------------------------------------
# Place order
# ---------------------------------------------------------------------------

def _place_order(
    conn: psycopg.Connection,
    copy_schema: str,
    payload: dict[str, Any],
    catalog_sql: CatalogSQL,
) -> dict[str, Any]:
    phone: str = payload["phone"]
    order_type: str = payload["order_type"]
    items: list[dict[str, Any]] = sorted(payload["items"], key=lambda i: i["product_id"])
    delivery_address: str | None = payload.get("delivery_address")
    pickup_time: str | None = payload.get("pickup_time")
    notes: str | None = payload.get("notes")

    # Deduct stock for every item (raises on insufficient stock)
    line_items: list[dict[str, Any]] = []
    for item in items:
        pid = item["product_id"]
        qty = item["quantity"]
        result = catalog_sql.deduct_stock(conn, pid, qty)
        if result is None:
            raise OrderError(
                f"Insufficient stock for product id {pid} "
                f"(requested {qty})."
            )
        name = result[catalog_sql.name_col]
        price = Decimal(str(result[catalog_sql.price_col]))
        line_items.append({
            "product_id": pid,
            "product_name": name,
            "unit_price": price,
            "quantity": qty,
            "line_total": price * qty,
        })

    total = sum(li["line_total"] for li in line_items)

    # Upsert customer by phone
    conn.execute(
        sql.SQL(
            "INSERT INTO {s}.customers (phone) VALUES (%s) "
            "ON CONFLICT (phone) DO NOTHING"
        ).format(s=_s(copy_schema)),
        (phone,),
    )
    cust = conn.execute(
        sql.SQL("SELECT id FROM {s}.customers WHERE phone = %s").format(
            s=_s(copy_schema)
        ),
        (phone,),
    ).fetchone()
    customer_id: int = cust["id"]

    order_number = generate_order_number()

    pickup_ts = None
    if pickup_time:
        from datetime import datetime as dt
        try:
            pickup_ts = dt.fromisoformat(pickup_time)
        except ValueError:
            pickup_ts = None

    order_row = conn.execute(
        sql.SQL(
            "INSERT INTO {s}.orders "
            "  (order_number, session_id, customer_id, order_type, "
            "   total_amount, delivery_address, pickup_time, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, order_number, status"
        ).format(s=_s(copy_schema)),
        (
            order_number,
            payload["session_id"],
            customer_id,
            order_type,
            total,
            delivery_address,
            pickup_ts,
            notes,
        ),
    ).fetchone()
    order_id: int = order_row["id"]

    for li in line_items:
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.order_items "
                "  (order_id, product_id, product_name, unit_price, quantity, line_total) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(s=_s(copy_schema)),
            (
                order_id,
                li["product_id"],
                li["product_name"],
                li["unit_price"],
                li["quantity"],
                li["line_total"],
            ),
        )

    conn.execute(
        sql.SQL(
            "INSERT INTO {s}.order_status_history "
            "  (order_id, old_status, new_status, note, changed_by) "
            "VALUES (%s, NULL, 'pending', 'Order placed', 'system')"
        ).format(s=_s(copy_schema)),
        (order_id,),
    )

    return {
        "order_number": order_number,
        "status": "pending",
        "total_amount": str(total),
        "items": [
            {
                "product_name": li["product_name"],
                "quantity": li["quantity"],
                "unit_price": str(li["unit_price"]),
                "line_total": str(li["line_total"]),
            }
            for li in line_items
        ],
    }


# ---------------------------------------------------------------------------
# Cancel order
# ---------------------------------------------------------------------------

def _cancel_order(
    conn: psycopg.Connection,
    copy_schema: str,
    payload: dict[str, Any],
    catalog_sql: CatalogSQL,
) -> dict[str, Any]:
    order_number: str = payload["order_number"]
    phone: str = payload["phone"]

    order = get_order(conn, copy_schema, order_number, phone)
    if order is None:
        raise OrderError(f"Order {order_number!r} not found for this customer.")

    current_status: str = order["status"]
    if "cancelled" not in VALID_TRANSITIONS.get(current_status, set()):
        raise OrderError(
            f"Order {order_number!r} cannot be cancelled from status {current_status!r}."
        )

    order_id: int = order["id"]

    # Restore stock
    items = conn.execute(
        sql.SQL(
            "SELECT product_id, quantity FROM {s}.order_items WHERE order_id = %s"
        ).format(s=_s(copy_schema)),
        (order_id,),
    ).fetchall()
    for item in items:
        catalog_sql.restore_stock(conn, item["product_id"], item["quantity"])

    # Handle refund if already paid
    payment_status: str = order["payment_status"]
    new_status = "cancelled"
    if payment_status == "paid":
        new_status = "refund_pending"
        conn.execute(
            sql.SQL(
                "UPDATE {s}.orders "
                "SET status = 'refund_pending', payment_status = 'refund_pending', "
                "    updated_at = now() "
                "WHERE id = %s"
            ).format(s=_s(copy_schema)),
            (order_id,),
        )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.refunds (order_id, amount, note) "
                "VALUES (%s, %s, 'Auto-created on cancellation of paid order')"
            ).format(s=_s(copy_schema)),
            (order_id, order["total_amount"]),
        )
    else:
        conn.execute(
            sql.SQL(
                "UPDATE {s}.orders SET status = 'cancelled', updated_at = now() "
                "WHERE id = %s"
            ).format(s=_s(copy_schema)),
            (order_id,),
        )

    conn.execute(
        sql.SQL(
            "INSERT INTO {s}.order_status_history "
            "  (order_id, old_status, new_status, note, changed_by) "
            "VALUES (%s, %s, %s, 'Cancelled by customer', 'customer')"
        ).format(s=_s(copy_schema)),
        (order_id, current_status, new_status),
    )

    return {"order_number": order_number, "status": new_status}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_order(
    conn: psycopg.Connection,
    copy_schema: str,
    order_number: str,
    phone: str,
) -> dict[str, Any] | None:
    """Return the order row, or None if not found or phone mismatches."""
    row = conn.execute(
        sql.SQL(
            "SELECT o.id, o.order_number, o.status, o.payment_status, "
            "       o.order_type, o.total_amount, o.delivery_address, "
            "       o.pickup_time, o.notes, o.created_at, o.session_id "
            "FROM {s}.orders o "
            "JOIN {s}.customers c ON c.id = o.customer_id "
            "WHERE o.order_number = %s AND c.phone = %s"
        ).format(s=_s(copy_schema)),
        (order_number, phone),
    ).fetchone()
    return dict(row) if row else None


def transition_order_status(
    conn: psycopg.Connection,
    copy_schema: str,
    order_number: str,
    new_status: str,
    note: str,
    changed_by: str,
) -> None:
    """Validate transition, update orders, and insert history row."""
    row = conn.execute(
        sql.SQL(
            "SELECT id, status FROM {s}.orders WHERE order_number = %s"
        ).format(s=_s(copy_schema)),
        (order_number,),
    ).fetchone()
    if row is None:
        raise OrderError(f"Order {order_number!r} not found.")

    current = row["status"]
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise OrderError(
            f"Cannot transition order from {current!r} to {new_status!r}."
        )

    order_id: int = row["id"]
    conn.execute(
        sql.SQL(
            "UPDATE {s}.orders SET status = %s, updated_at = now() WHERE id = %s"
        ).format(s=_s(copy_schema)),
        (new_status, order_id),
    )
    conn.execute(
        sql.SQL(
            "INSERT INTO {s}.order_status_history "
            "  (order_id, old_status, new_status, note, changed_by) "
            "VALUES (%s, %s, %s, %s, %s)"
        ).format(s=_s(copy_schema)),
        (order_id, current, new_status, note, changed_by),
    )
