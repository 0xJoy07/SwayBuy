"""Source schema introspection and mapping proposal/validation.

All source queries run inside BEGIN READ ONLY transactions.
No secrets are passed to the LLM path — only schema metadata.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

STATE_DIR = Path(__file__).resolve().parents[1] / "state"
MAPPING_PATH = STATE_DIR / "mapping.json"
PROPOSED_PATH = STATE_DIR / "mapping.proposed.json"

# ---------------------------------------------------------------------------
# Name-similarity heuristics
# ---------------------------------------------------------------------------

_PRODUCT_HINTS = {"product", "item", "sku", "goods", "article", "merchandise",
                  "catalog", "catalogue", "inventory", "stock"}
_ORDER_HINTS = {"order", "purchase", "sale", "transaction", "basket", "cart"}
_REVIEW_HINTS = {"review", "rating", "feedback", "comment", "opinion"}

_COL_ROLES: dict[str, list[str]] = {
    "id":       ["id", "pk", "product_id", "item_id", "sku_id"],
    "name":     ["name", "title", "label", "description", "product_name",
                 "item_name", "sku_name"],
    "price":    ["price", "cost", "unit_price", "sale_price", "amount",
                 "mrp", "rate"],
    "stock":    ["stock", "quantity", "qty", "inventory", "units",
                 "available", "stock_quantity", "units_available"],
    "category": ["category", "type", "group", "department", "section",
                 "kind", "class"],
    "review_id":    ["id", "pk", "review_id"],
    "product_ref":  ["product_id", "item_id", "sku_id", "product_fk"],
    "rating":       ["rating", "score", "stars", "grade"],
    "review_text":  ["review", "text", "body", "comment", "feedback",
                     "description", "content"],
}


class MappingError(Exception):
    """Raised when mapping.json is missing or structurally invalid."""


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def introspect_schema(pool: ConnectionPool, schema: str) -> dict[str, Any]:
    """Read table/column/PK/FK/row-count metadata from information_schema."""
    result: dict[str, Any] = {"schema": schema, "tables": {}}

    with pool.connection() as conn:
        conn.execute("BEGIN READ ONLY")

        tables = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema,),
        ).fetchall()

        for row in tables:
            tname: str = row["table_name"]

            cols = conn.execute(
                """
                SELECT column_name,
                       data_type,
                       udt_name,
                       is_nullable,
                       column_default
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema, tname),
            ).fetchall()

            pks = conn.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                 AND kcu.table_schema    = tc.table_schema
                WHERE tc.table_schema      = %s
                  AND tc.table_name        = %s
                  AND tc.constraint_type   = 'PRIMARY KEY'
                ORDER BY kcu.ordinal_position
                """,
                (schema, tname),
            ).fetchall()

            fks = conn.execute(
                """
                SELECT kcu.column_name,
                       ccu.table_name  AS foreign_table,
                       ccu.column_name AS foreign_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                 AND kcu.table_schema    = tc.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                WHERE tc.table_schema    = %s
                  AND tc.table_name      = %s
                  AND tc.constraint_type = 'FOREIGN KEY'
                """,
                (schema, tname),
            ).fetchall()

            try:
                count_row = conn.execute(
                    f'SELECT count(*) AS n FROM "{schema}"."{tname}"'
                ).fetchone()
                row_count: int = count_row["n"] if count_row else 0
            except Exception:
                row_count = -1

        result["tables"][tname] = {
                "columns": [dict(c) for c in cols],
                "primary_keys": [r["column_name"] for r in pks],
                "foreign_keys": [dict(f) for f in fks],
                "row_count": row_count,
            }

        conn.execute("ROLLBACK")

    return result


# ---------------------------------------------------------------------------
# Mapping proposal
# ---------------------------------------------------------------------------

def _score_table(tname: str, hints: set[str]) -> int:
    tl = tname.lower()
    return sum(1 for h in hints if h in tl)


def _score_column(cname: str, candidates: list[str]) -> int:
    cl = cname.lower()
    for i, cand in enumerate(candidates):
        if cl == cand:
            return len(candidates) - i + 10
        if cand in cl:
            return len(candidates) - i
    return 0


def _best_col(
    columns: list[dict[str, Any]],
    role: str,
    require_numeric: bool = False,
) -> str | None:
    scored = []
    for col in columns:
        cname = col["column_name"]
        dtype = col["data_type"].lower()
        udt = col["udt_name"].lower()
        if require_numeric:
            if not any(
                t in dtype or t in udt
                for t in ("int", "numeric", "decimal", "float", "double",
                           "real", "money", "bigint", "smallint")
            ):
                continue
        s = _score_column(cname, _COL_ROLES.get(role, []))
        if s > 0:
            scored.append((s, cname))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][1]


def propose_mapping(
    schema_info: dict[str, Any],
    use_llm: bool = False,
    groq_api_key: str | None = None,
    groq_model: str = "llama-3.3-70b-versatile",
) -> dict[str, Any]:
    """Build a mapping proposal from schema_info using deterministic heuristics.

    If use_llm=True and groq_api_key is provided, the LLM is asked to choose
    among the real column names only — it cannot invent names.
    """
    tables = schema_info.get("tables", {})
    needs_review: list[str] = []

    # --- products table ---
    prod_candidates = sorted(
        tables.keys(),
        key=lambda t: _score_table(t, _PRODUCT_HINTS),
        reverse=True,
    )
    products_table = prod_candidates[0] if prod_candidates else None
    products: dict[str, Any] = {}
    if products_table:
        cols = tables[products_table]["columns"]
        pks = tables[products_table]["primary_keys"]

        id_col = _best_col(cols, "id") or (pks[0] if pks else None)
        name_col = _best_col(cols, "name")
        price_col = _best_col(cols, "price", require_numeric=True)
        stock_col = _best_col(cols, "stock", require_numeric=True)
        category_col = _best_col(cols, "category")

        products = {
            "table": products_table,
            "id_col": id_col,
            "name_col": name_col,
            "price_col": price_col,
            "stock_col": stock_col,
            "category_col": category_col,
        }
        if not name_col:
            needs_review.append("products.name_col — could not detect automatically")
        if not price_col:
            needs_review.append("products.price_col — could not detect automatically")
        if not stock_col:
            needs_review.append("products.stock_col — could not detect automatically")

    # --- reviews table (optional) ---
    rev_candidates = sorted(
        tables.keys(),
        key=lambda t: _score_table(t, _REVIEW_HINTS),
        reverse=True,
    )
    reviews: dict[str, Any] | None = None
    if rev_candidates and _score_table(rev_candidates[0], _REVIEW_HINTS) > 0:
        rtname = rev_candidates[0]
        rcols = tables[rtname]["columns"]
        reviews = {
            "table": rtname,
            "review_id_col": _best_col(rcols, "review_id"),
            "product_ref_col": _best_col(rcols, "product_ref"),
            "rating_col": _best_col(rcols, "rating", require_numeric=True),
            "review_text_col": _best_col(rcols, "review_text"),
        }

    proposal: dict[str, Any] = {
        "products": products,
        "needs_review": needs_review,
    }
    if reviews:
        proposal["reviews"] = reviews

    if use_llm and groq_api_key:
        proposal = _refine_with_llm(
            proposal, schema_info, groq_api_key, groq_model
        )

    return proposal


def _refine_with_llm(
    proposal: dict[str, Any],
    schema_info: dict[str, Any],
    groq_api_key: str,
    groq_model: str,
) -> dict[str, Any]:
    """Ask the LLM to pick among real column names only."""
    from groq import Groq

    tables = schema_info.get("tables", {})
    schema_summary = json.dumps(
        {
            tname: {
                "columns": [c["column_name"] for c in tdata["columns"]],
                "primary_keys": tdata["primary_keys"],
            }
            for tname, tdata in tables.items()
        },
        indent=2,
    )

    prompt = (
        "You are a database schema analyst. "
        "Given the following Postgres schema, identify the best mapping for a "
        "shopping catalog agent. "
        "You MUST only use column names that actually exist in the schema. "
        "Return ONLY a valid JSON object with the same structure as the proposal "
        "below, filling in the correct column names from the schema. "
        "Do not invent or guess names.\n\n"
        f"Schema:\n{schema_summary}\n\n"
        f"Current proposal (improve if needed):\n{json.dumps(proposal, indent=2)}"
    )

    client = Groq(api_key=groq_api_key)
    response = client.chat.completions.create(
        model=groq_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=1024,
    )
    raw = response.choices[0].message.content or ""
    # Extract JSON block
    match = re.search(r"\{[\s\S]+\}", raw)
    if match:
        try:
            llm_proposal = json.loads(match.group())
            # Safety: validate every value is a real column name
            all_col_names: set[str] = set()
            all_table_names = set(tables.keys())
            for tdata in tables.values():
                for col in tdata["columns"]:
                    all_col_names.add(col["column_name"])

            def _sanitize(obj: Any) -> Any:
                if isinstance(obj, str):
                    return obj if (obj in all_col_names or obj in all_table_names) else None
                if isinstance(obj, dict):
                    return {k: _sanitize(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_sanitize(i) for i in obj]
                return obj

            return _sanitize(llm_proposal)  # type: ignore[return-value]
        except (json.JSONDecodeError, ValueError):
            pass
    return proposal


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = {
    "integer", "bigint", "smallint", "numeric", "decimal",
    "real", "double precision", "money",
}


def validate_mapping(
    mapping: dict[str, Any],
    schema_info: dict[str, Any],
) -> list[str]:
    """Return a list of error strings; empty list means valid."""
    errors: list[str] = []
    tables = schema_info.get("tables", {})
    prod = mapping.get("products", {})

    prod_table = prod.get("table")
    if not prod_table:
        errors.append("products.table is missing")
        return errors
    if prod_table not in tables:
        errors.append(f"products.table {prod_table!r} not found in source schema")
        return errors

    tinfo = tables[prod_table]
    col_map = {c["column_name"]: c for c in tinfo["columns"]}
    pks = set(tinfo["primary_keys"])

    for role in ("id_col", "name_col", "price_col", "stock_col"):
        cname = prod.get(role)
        if not cname:
            errors.append(f"products.{role} is missing")
            continue
        if cname not in col_map:
            errors.append(
                f"products.{role} = {cname!r} not found in table {prod_table!r}"
            )
            continue
        if role in ("price_col", "stock_col"):
            dtype = col_map[cname]["data_type"].lower()
            udt = col_map[cname]["udt_name"].lower()
            if not any(
                t in dtype or t in udt
                for t in ("int", "numeric", "decimal", "float", "double",
                           "real", "money", "bigint", "smallint")
            ):
                errors.append(
                    f"products.{role} = {cname!r} has non-numeric type "
                    f"{col_map[cname]['data_type']!r}"
                )

    id_col = prod.get("id_col")
    if id_col and id_col not in pks:
        errors.append(
            f"products.id_col = {id_col!r} is not part of the primary key "
            f"for table {prod_table!r}"
        )

    reviews = mapping.get("reviews")
    if reviews:
        rtable = reviews.get("table")
        if rtable and rtable not in tables:
            errors.append(f"reviews.table {rtable!r} not found in source schema")
        elif rtable:
            rcols = {c["column_name"] for c in tables[rtable]["columns"]}
            for role in ("review_id_col", "product_ref_col", "rating_col"):
                cname = reviews.get(role)
                if cname and cname not in rcols:
                    errors.append(
                        f"reviews.{role} = {cname!r} not found in table {rtable!r}"
                    )

    return errors


# ---------------------------------------------------------------------------
# Load confirmed mapping
# ---------------------------------------------------------------------------

def load_mapping(require_confirmed: bool = True) -> dict[str, Any]:
    if not MAPPING_PATH.exists():
        raise MappingError(
            "mapping.json not found. Run `python main.py scan` then "
            "`python main.py confirm-mapping`."
        )
    try:
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MappingError(f"mapping.json is not valid JSON: {exc}") from exc

    if require_confirmed and not mapping.get("confirmed_at"):
        raise MappingError(
            "mapping.json has not been confirmed. "
            "Run `python main.py confirm-mapping`."
        )
    if "products" not in mapping or not mapping["products"].get("table"):
        raise MappingError("mapping.json is missing required products.table.")
    return mapping
