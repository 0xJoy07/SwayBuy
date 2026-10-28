"""Unit tests for scanner.py — no database required."""
from __future__ import annotations

import pytest
from db.scanner import MappingError, propose_mapping, validate_mapping


# ---------------------------------------------------------------------------
# Helpers: build minimal schema_info dicts
# ---------------------------------------------------------------------------

def _make_schema(
    tables: dict,
) -> dict:
    return {"schema": "public", "tables": tables}


def _make_table(
    columns: list[dict],
    primary_keys: list[str] | None = None,
    foreign_keys: list[dict] | None = None,
    row_count: int = 0,
) -> dict:
    return {
        "columns": columns,
        "primary_keys": primary_keys or [],
        "foreign_keys": foreign_keys or [],
        "row_count": row_count,
    }


def _col(name: str, dtype: str = "text", udt: str | None = None) -> dict:
    return {
        "column_name": name,
        "data_type": dtype,
        "udt_name": udt or dtype,
        "is_nullable": "YES",
        "column_default": None,
    }


def _num_col(name: str) -> dict:
    return _col(name, "numeric", "numeric")


def _int_col(name: str) -> dict:
    return _col(name, "integer", "int4")


# ---------------------------------------------------------------------------
# propose_mapping
# ---------------------------------------------------------------------------

def test_propose_mapping_detects_products_table() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[
                _int_col("id"),
                _col("name"),
                _num_col("price"),
                _int_col("stock"),
            ],
            primary_keys=["id"],
        )
    })
    proposal = propose_mapping(schema)
    assert proposal["products"]["table"] == "products"
    assert proposal["products"]["id_col"] == "id"
    assert proposal["products"]["name_col"] == "name"
    assert proposal["products"]["price_col"] == "price"
    assert proposal["products"]["stock_col"] == "stock"


def test_propose_mapping_detects_reviews_table() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[_int_col("id"), _col("name"), _num_col("price"), _int_col("stock")],
            primary_keys=["id"],
        ),
        "reviews": _make_table(
            columns=[
                _int_col("id"),
                _int_col("product_id"),
                _num_col("rating"),
                _col("review"),
            ],
            primary_keys=["id"],
        ),
    })
    proposal = propose_mapping(schema)
    assert "reviews" in proposal
    assert proposal["reviews"]["table"] == "reviews"
    assert proposal["reviews"]["rating_col"] == "rating"


def test_propose_mapping_flags_missing_price_col() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[_int_col("id"), _col("name"), _col("description")],
            primary_keys=["id"],
        )
    })
    proposal = propose_mapping(schema)
    needs = proposal.get("needs_review", [])
    assert any("price_col" in n for n in needs)


# ---------------------------------------------------------------------------
# validate_mapping — rejections
# ---------------------------------------------------------------------------

def test_validate_missing_table_key() -> None:
    mapping = {"products": {}}
    schema = _make_schema({})
    errors = validate_mapping(mapping, schema)
    assert any("products.table is missing" in e for e in errors)


def test_validate_table_not_in_schema() -> None:
    mapping = {"products": {"table": "nonexistent"}}
    schema = _make_schema({})
    errors = validate_mapping(mapping, schema)
    assert any("not found in source schema" in e for e in errors)


def test_validate_non_numeric_price() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[
                _int_col("id"),
                _col("name"),
                _col("price"),          # text — should fail
                _int_col("stock"),
            ],
            primary_keys=["id"],
        )
    })
    mapping = {
        "products": {
            "table": "products",
            "id_col": "id",
            "name_col": "name",
            "price_col": "price",
            "stock_col": "stock",
        }
    }
    errors = validate_mapping(mapping, schema)
    assert any("price_col" in e and "non-numeric" in e for e in errors)


def test_validate_non_numeric_stock() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[
                _int_col("id"),
                _col("name"),
                _num_col("price"),
                _col("stock"),          # text — should fail
            ],
            primary_keys=["id"],
        )
    })
    mapping = {
        "products": {
            "table": "products",
            "id_col": "id",
            "name_col": "name",
            "price_col": "price",
            "stock_col": "stock",
        }
    }
    errors = validate_mapping(mapping, schema)
    assert any("stock_col" in e and "non-numeric" in e for e in errors)


def test_validate_id_not_in_pk() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[
                _int_col("id"),
                _col("name"),
                _num_col("price"),
                _int_col("stock"),
            ],
            primary_keys=["id"],
        )
    })
    mapping = {
        "products": {
            "table": "products",
            "id_col": "name",           # not a PK
            "name_col": "name",
            "price_col": "price",
            "stock_col": "stock",
        }
    }
    errors = validate_mapping(mapping, schema)
    assert any("not part of the primary key" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_mapping — acceptance
# ---------------------------------------------------------------------------

def test_validate_valid_mapping_returns_no_errors() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[
                _int_col("id"),
                _col("name"),
                _num_col("price"),
                _int_col("stock"),
            ],
            primary_keys=["id"],
        )
    })
    mapping = {
        "products": {
            "table": "products",
            "id_col": "id",
            "name_col": "name",
            "price_col": "price",
            "stock_col": "stock",
        }
    }
    errors = validate_mapping(mapping, schema)
    assert errors == []


def test_validate_valid_mapping_with_category_and_reviews() -> None:
    schema = _make_schema({
        "products": _make_table(
            columns=[
                _int_col("id"),
                _col("name"),
                _num_col("price"),
                _int_col("stock"),
                _col("category"),
            ],
            primary_keys=["id"],
        ),
        "reviews": _make_table(
            columns=[
                _int_col("id"),
                _int_col("product_id"),
                _num_col("rating"),
                _col("review"),
            ],
            primary_keys=["id"],
        ),
    })
    mapping = {
        "products": {
            "table": "products",
            "id_col": "id",
            "name_col": "name",
            "price_col": "price",
            "stock_col": "stock",
            "category_col": "category",
        },
        "reviews": {
            "table": "reviews",
            "review_id_col": "id",
            "product_ref_col": "product_id",
            "rating_col": "rating",
            "review_text_col": "review",
        },
    }
    errors = validate_mapping(mapping, schema)
    assert errors == []


# ---------------------------------------------------------------------------
# load_mapping raises MappingError when file is absent
# ---------------------------------------------------------------------------

def test_load_mapping_raises_when_absent(tmp_path, monkeypatch) -> None:
    import db.scanner as scanner_mod
    monkeypatch.setattr(scanner_mod, "MAPPING_PATH", tmp_path / "mapping.json")
    with pytest.raises(MappingError, match="not found"):
        scanner_mod.load_mapping(require_confirmed=True)


def test_load_mapping_raises_when_not_confirmed(tmp_path, monkeypatch) -> None:
    import db.scanner as scanner_mod
    import json
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"products": {"table": "t"}}), encoding="utf-8")
    monkeypatch.setattr(scanner_mod, "MAPPING_PATH", path)
    with pytest.raises(MappingError, match="not been confirmed"):
        scanner_mod.load_mapping(require_confirmed=True)
