"""Catalog queries against the copy schema.

All SQL is fixed and parameterized. Table/column names come from the validated
mapping and are inserted via psycopg.sql.Identifier — never string-formatted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool


@dataclass
class CatalogSQL:
    copy_schema: str
    products_table: str
    id_col: str
    name_col: str
    price_col: str
    stock_col: str
    category_col: str | None = None
    reviews_table: str | None = None
    review_id_col: str | None = None
    product_ref_col: str | None = None
    rating_col: str | None = None
    review_text_col: str | None = None

    # -------------------------------------------------------------------------
    # Schema identifiers (built once, reused)
    # -------------------------------------------------------------------------

    @property
    def _s(self) -> sql.Identifier:
        return sql.Identifier(self.copy_schema)

    @property
    def _t(self) -> sql.Identifier:
        return sql.Identifier(self.products_table)

    def _col(self, name: str) -> sql.Identifier:
        return sql.Identifier(name)

    # -------------------------------------------------------------------------
    # Public methods
    # -------------------------------------------------------------------------

    def search(
        self,
        conn: psycopg.Connection,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Full-text ILIKE search across name and optional category."""
        pattern = f"%{query}%"
        extra_col = (
            sql.SQL(", {cat}").format(cat=self._col(self.category_col))
            if self.category_col
            else sql.SQL("")
        )
        rows = conn.execute(
            sql.SQL(
                "SELECT {id}, {name}, {price}, {stock}{extra} "
                "FROM {s}.{t} "
                "WHERE {name} ILIKE %s "
                "ORDER BY {name} LIMIT %s"
            ).format(
                id=self._col(self.id_col),
                name=self._col(self.name_col),
                price=self._col(self.price_col),
                stock=self._col(self.stock_col),
                extra=extra_col,
                s=self._s,
                t=self._t,
            ),
            (pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def check_availability(
        self,
        conn: psycopg.Connection,
        product_id: int,
    ) -> dict[str, Any] | None:
        """Return product row with in_stock flag, or None if not found."""
        row = conn.execute(
            sql.SQL(
                "SELECT {id}, {name}, {price}, {stock} FROM {s}.{t} WHERE {id} = %s"
            ).format(
                id=self._col(self.id_col),
                name=self._col(self.name_col),
                price=self._col(self.price_col),
                stock=self._col(self.stock_col),
                s=self._s,
                t=self._t,
            ),
            (product_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["in_stock"] = int(d[self.stock_col]) > 0
        return d

    def find_alternatives(
        self,
        conn: psycopg.Connection,
        product_id: int,
        price_tolerance: float = 0.35,
    ) -> list[dict[str, Any]]:
        """Return in-stock products in the same category within ±35% price."""
        ref = self.check_availability(conn, product_id)
        if ref is None:
            return []

        ref_price = Decimal(str(ref[self.price_col]))
        low = ref_price * Decimal(str(1 - price_tolerance))
        high = ref_price * Decimal(str(1 + price_tolerance))
        ref_cat = ref.get(self.category_col) if self.category_col else None

        if self.category_col and ref_cat is not None:
            rows = conn.execute(
                sql.SQL(
                    "SELECT {id}, {name}, {price}, {stock}, {cat} "
                    "FROM {s}.{t} "
                    "WHERE {id} <> %s "
                    "  AND {stock} > 0 "
                    "  AND {cat} = %s "
                    "  AND {price} BETWEEN %s AND %s "
                    "ORDER BY {price} LIMIT 5"
                ).format(
                    id=self._col(self.id_col),
                    name=self._col(self.name_col),
                    price=self._col(self.price_col),
                    stock=self._col(self.stock_col),
                    cat=self._col(self.category_col),
                    s=self._s,
                    t=self._t,
                ),
                (product_id, ref_cat, low, high),
            ).fetchall()
        else:
            rows = conn.execute(
                sql.SQL(
                    "SELECT {id}, {name}, {price}, {stock} "
                    "FROM {s}.{t} "
                    "WHERE {id} <> %s "
                    "  AND {stock} > 0 "
                    "  AND {price} BETWEEN %s AND %s "
                    "ORDER BY {price} LIMIT 5"
                ).format(
                    id=self._col(self.id_col),
                    name=self._col(self.name_col),
                    price=self._col(self.price_col),
                    stock=self._col(self.stock_col),
                    s=self._s,
                    t=self._t,
                ),
                (product_id, low, high),
            ).fetchall()

        return [dict(r) for r in rows]

    def get_review_summary(
        self,
        conn: psycopg.Connection,
        product_id: int,
    ) -> dict[str, Any] | None:
        """Return avg rating + review count. Returns None if no reviews table."""
        if not self.reviews_table or not self.rating_col or not self.product_ref_col:
            return None

        row = conn.execute(
            sql.SQL(
                "SELECT count(*) AS review_count, "
                "       round(avg({rating})::numeric, 2) AS avg_rating "
                "FROM {s}.{rt} WHERE {ref} = %s"
            ).format(
                rating=self._col(self.rating_col),
                s=self._s,
                rt=sql.Identifier(self.reviews_table),
                ref=self._col(self.product_ref_col),
            ),
            (product_id,),
        ).fetchone()
        if row is None or row["review_count"] == 0:
            return {"review_count": 0, "avg_rating": None}

        texts: list[str] = []
        if self.review_text_col:
            text_rows = conn.execute(
                sql.SQL(
                    "SELECT {txt} FROM {s}.{rt} "
                    "WHERE {ref} = %s "
                    "ORDER BY {rid} DESC LIMIT 3"
                ).format(
                    txt=self._col(self.review_text_col),
                    s=self._s,
                    rt=sql.Identifier(self.reviews_table),
                    ref=self._col(self.product_ref_col),
                    rid=self._col(self.review_id_col or self.product_ref_col),
                ),
                (product_id,),
            ).fetchall()
            texts = [r[self.review_text_col] for r in text_rows if r[self.review_text_col]]

        return {
            "review_count": row["review_count"],
            "avg_rating": float(row["avg_rating"]) if row["avg_rating"] else None,
            "recent_snippets": texts,
        }

    def deduct_stock(
        self,
        conn: psycopg.Connection,
        product_id: int,
        quantity: int,
    ) -> dict[str, Any] | None:
        """Conditional UPDATE: deduct stock only if stock >= quantity.

        Returns the product name + unit_price on success, None if stock was
        insufficient (0 rows updated).
        """
        row = conn.execute(
            sql.SQL(
                "UPDATE {s}.{t} SET {stock} = {stock} - %s "
                "WHERE {id} = %s AND {stock} >= %s "
                "RETURNING {name}, {price}"
            ).format(
                s=self._s,
                t=self._t,
                stock=self._col(self.stock_col),
                id=self._col(self.id_col),
                name=self._col(self.name_col),
                price=self._col(self.price_col),
            ),
            (quantity, product_id, quantity),
        ).fetchone()
        return dict(row) if row else None

    def restore_stock(
        self,
        conn: psycopg.Connection,
        product_id: int,
        quantity: int,
    ) -> None:
        """Unconditionally add quantity back to stock."""
        conn.execute(
            sql.SQL(
                "UPDATE {s}.{t} SET {stock} = {stock} + %s WHERE {id} = %s"
            ).format(
                s=self._s,
                t=self._t,
                stock=self._col(self.stock_col),
                id=self._col(self.id_col),
            ),
            (quantity, product_id),
        )


def catalog_from_mapping(mapping: dict[str, Any], copy_schema: str) -> CatalogSQL:
    """Build a CatalogSQL from a confirmed mapping dict."""
    prod = mapping["products"]
    reviews = mapping.get("reviews") or {}
    return CatalogSQL(
        copy_schema=copy_schema,
        products_table=prod["table"],
        id_col=prod["id_col"],
        name_col=prod["name_col"],
        price_col=prod["price_col"],
        stock_col=prod["stock_col"],
        category_col=prod.get("category_col"),
        reviews_table=reviews.get("table"),
        review_id_col=reviews.get("review_id_col"),
        product_ref_col=reviews.get("product_ref_col"),
        rating_col=reviews.get("rating_col"),
        review_text_col=reviews.get("review_text_col"),
    )
