"""Order-flow tools: prepare_order, prepare_cancel, get_order_status,
request_return, get_return_status.

Tools only create pending_actions rows or query the DB — they never confirm
or write orders directly.  CLI confirmation (y/n) is the only write gate.

session_id and customer phone are injected by the CLI via AgentContext /
confirmed payload — they are never model arguments.
"""
from __future__ import annotations

import functools
import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator, model_validator

from agent.context import AgentContext


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"^\+?[\d\s\-]{7,15}$")


class OrderItem(BaseModel):
    product_id: int = Field(description="Numeric product ID.")
    quantity: int = Field(description="Number of units to order (must be > 0).")

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v


class PrepareOrderInput(BaseModel):
    phone: str = Field(
        description="Customer phone number for identification and order lookup."
    )
    order_type: str = Field(
        description="Either 'delivery' or 'takeaway'."
    )
    items: list[OrderItem] = Field(
        description="List of products and quantities to order."
    )
    delivery_address: str | None = Field(
        default=None,
        description="Required when order_type is 'delivery'.",
    )
    pickup_time: str | None = Field(
        default=None,
        description=(
            "Required when order_type is 'takeaway'. "
            "ISO-8601 datetime string (e.g. 2026-10-28T14:00:00)."
        ),
    )
    notes: str | None = Field(default=None, description="Optional order notes.")

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v.strip()):
            raise ValueError(
                "phone must be a valid phone number (7–15 digits, optional +/spaces/dashes)"
            )
        return v.strip()

    @field_validator("order_type")
    @classmethod
    def valid_order_type(cls, v: str) -> str:
        if v not in ("delivery", "takeaway"):
            raise ValueError("order_type must be 'delivery' or 'takeaway'")
        return v

    @model_validator(mode="after")
    def delivery_needs_address(self) -> "PrepareOrderInput":
        if self.order_type == "delivery" and not self.delivery_address:
            raise ValueError("delivery_address is required for delivery orders")
        return self

    @model_validator(mode="after")
    def takeaway_needs_future_pickup(self) -> "PrepareOrderInput":
        if self.order_type == "takeaway":
            if not self.pickup_time:
                raise ValueError("pickup_time is required for takeaway orders")
            try:
                pt = datetime.fromisoformat(self.pickup_time)
                if pt.tzinfo is None:
                    pt = pt.replace(tzinfo=timezone.utc)
                if pt <= datetime.now(tz=timezone.utc):
                    raise ValueError("pickup_time must be in the future")
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        return self


class PrepareCancelInput(BaseModel):
    order_number: str = Field(description="Order number to cancel (e.g. ORD-20261028-AB12).")
    phone: str = Field(description="Customer phone number used when placing the order.")

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v.strip()):
            raise ValueError("phone must be a valid phone number")
        return v.strip()


class GetOrderStatusInput(BaseModel):
    order_number: str = Field(description="Order number to look up.")
    phone: str = Field(description="Customer phone number for identity verification.")

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v.strip()):
            raise ValueError("phone must be a valid phone number")
        return v.strip()


class RequestReturnInput(BaseModel):
    order_number: str = Field(description="Order number for the return request.")
    phone: str = Field(description="Customer phone number for identity verification.")
    order_item_id: int = Field(description="ID of the specific order item to return.")
    quantity: int = Field(description="Number of units to return (must be > 0).")
    reason: str | None = Field(default=None, description="Optional reason for the return.")

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v.strip()):
            raise ValueError("phone must be a valid phone number")
        return v.strip()


class GetReturnStatusInput(BaseModel):
    return_id: int = Field(description="Return request ID to look up.")
    phone: str = Field(description="Customer phone number for identity verification.")

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v.strip()):
            raise ValueError("phone must be a valid phone number")
        return v.strip()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _prepare_order_impl(
    phone: str,
    order_type: str,
    items: list[dict[str, Any]],
    delivery_address: str | None = None,
    pickup_time: str | None = None,
    notes: str | None = None,
    *,
    _ctx: AgentContext,
) -> dict[str, Any]:
    from db.orders_repo import create_pending_action

    stock_issues: list[str] = []
    with _ctx.copy_pool.connection() as conn:
        for item in items:
            pid = item["product_id"]
            qty = item["quantity"]
            avail = _ctx.catalog_sql.check_availability(conn, pid)
            if avail is None:
                stock_issues.append(f"Product id {pid} not found.")
            elif int(avail[_ctx.catalog_sql.stock_col]) < qty:
                stock_issues.append(
                    f"Insufficient stock for product id {pid}: "
                    f"requested {qty}, available {avail[_ctx.catalog_sql.stock_col]}."
                )

    if stock_issues:
        return {"error": True, "stock_issues": stock_issues}

    payload: dict[str, Any] = {
        "session_id": _ctx.session_id,
        "phone": phone,
        "order_type": order_type,
        "items": items,
        "delivery_address": delivery_address,
        "pickup_time": pickup_time,
        "notes": notes,
    }

    with _ctx.copy_pool.connection() as conn:
        token = create_pending_action(
            conn,
            _ctx.settings.copy_schema,
            _ctx.session_id,
            "place_order",
            payload,
            _ctx.settings.confirmation_ttl_minutes,
        )

    return {
        "requires_confirmation": True,
        "token": str(token),
        "summary": {
            "order_type": order_type,
            "phone": phone,
            "items": items,
            "delivery_address": delivery_address,
            "pickup_time": pickup_time,
            "notes": notes,
        },
    }


def _prepare_cancel_impl(
    order_number: str,
    phone: str,
    *,
    _ctx: AgentContext,
) -> dict[str, Any]:
    from db.orders_repo import create_pending_action, get_order, VALID_TRANSITIONS

    with _ctx.copy_pool.connection() as conn:
        order = get_order(conn, _ctx.settings.copy_schema, order_number, phone)

    if order is None:
        return {"found": False, "order_number": order_number}

    if "cancelled" not in VALID_TRANSITIONS.get(order["status"], set()):
        return {
            "cancellable": False,
            "order_number": order_number,
            "status": order["status"],
            "message": f"Order cannot be cancelled from status '{order['status']}'.",
        }

    payload = {"order_number": order_number, "phone": phone}
    with _ctx.copy_pool.connection() as conn:
        token = create_pending_action(
            conn,
            _ctx.settings.copy_schema,
            _ctx.session_id,
            "cancel_order",
            payload,
            _ctx.settings.confirmation_ttl_minutes,
        )

    return {
        "requires_confirmation": True,
        "token": str(token),
        "order_number": order_number,
        "current_status": order["status"],
    }


def _get_order_status_impl(
    order_number: str,
    phone: str,
    *,
    _ctx: AgentContext,
) -> dict[str, Any] | None:
    from db.orders_repo import get_order
    from decimal import Decimal

    with _ctx.copy_pool.connection() as conn:
        order = get_order(conn, _ctx.settings.copy_schema, order_number, phone)

    if order is None:
        return None

    return {
        k: str(v) if isinstance(v, Decimal) else
           v.isoformat() if hasattr(v, "isoformat") else v
        for k, v in order.items()
    }


def _request_return_impl(
    order_number: str,
    phone: str,
    order_item_id: int,
    quantity: int,
    reason: str | None = None,
    *,
    _ctx: AgentContext,
) -> dict[str, Any]:
    from db.returns_repo import request_return, ReturnError

    try:
        with _ctx.copy_pool.connection() as conn:
            result = request_return(
                conn,
                _ctx.settings.copy_schema,
                order_number,
                phone,
                order_item_id,
                quantity,
                reason,
                _ctx.settings.return_window_days,
            )
        return result
    except ReturnError as exc:
        return {"error": True, "message": str(exc)}


def _get_return_status_impl(
    return_id: int,
    phone: str,
    *,
    _ctx: AgentContext,
) -> dict[str, Any] | None:
    from db.returns_repo import get_return_status

    with _ctx.copy_pool.connection() as conn:
        result = get_return_status(conn, _ctx.settings.copy_schema, return_id, phone)

    if result is None:
        return None

    return {
        k: v.isoformat() if hasattr(v, "isoformat") else v
        for k, v in result.items()
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_order_tools(ctx: AgentContext) -> list[BaseTool]:

    @functools.wraps(_prepare_order_impl)
    def prepare_order(
        phone: str,
        order_type: str,
        items: list[dict[str, Any]],
        delivery_address: str | None = None,
        pickup_time: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return _prepare_order_impl(
            phone, order_type, items,
            delivery_address, pickup_time, notes,
            _ctx=ctx,
        )

    @functools.wraps(_prepare_cancel_impl)
    def prepare_cancel(order_number: str, phone: str) -> dict[str, Any]:
        return _prepare_cancel_impl(order_number, phone, _ctx=ctx)

    @functools.wraps(_get_order_status_impl)
    def get_order_status(order_number: str, phone: str) -> dict[str, Any] | None:
        return _get_order_status_impl(order_number, phone, _ctx=ctx)

    @functools.wraps(_request_return_impl)
    def request_return(
        order_number: str,
        phone: str,
        order_item_id: int,
        quantity: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return _request_return_impl(
            order_number, phone, order_item_id, quantity, reason, _ctx=ctx
        )

    @functools.wraps(_get_return_status_impl)
    def get_return_status(return_id: int, phone: str) -> dict[str, Any] | None:
        return _get_return_status_impl(return_id, phone, _ctx=ctx)

    return [
        StructuredTool.from_function(
            func=prepare_order,
            name="prepare_order",
            description=(
                "Validate stock and create a pending order for CLI confirmation. "
                "Returns requires_confirmation=True and a token. "
                "The order is NOT placed until the customer types 'y' in the terminal. "
                "Never confirm on the customer's behalf."
            ),
            args_schema=PrepareOrderInput,
        ),
        StructuredTool.from_function(
            func=prepare_cancel,
            name="prepare_cancel",
            description=(
                "Prepare a cancellation for CLI confirmation. "
                "Returns requires_confirmation=True and a token. "
                "The order is NOT cancelled until the customer types 'y'."
            ),
            args_schema=PrepareCancelInput,
        ),
        StructuredTool.from_function(
            func=get_order_status,
            name="get_order_status",
            description=(
                "Look up the current status of an order. "
                "Returns None when the order is not found or phone does not match."
            ),
            args_schema=GetOrderStatusInput,
        ),
        StructuredTool.from_function(
            func=request_return,
            name="request_return",
            description=(
                "Submit a return request for a completed order item. "
                "Validates the return window and quantity constraints. "
                "Returns error=True with a message on failure."
            ),
            args_schema=RequestReturnInput,
        ),
        StructuredTool.from_function(
            func=get_return_status,
            name="get_return_status",
            description=(
                "Look up the status of a return request by its ID. "
                "Returns None when the return is not found or phone does not match."
            ),
            args_schema=GetReturnStatusInput,
        ),
    ]
