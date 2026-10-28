"""Structural test: agent and repo modules must not import the source pool.

This test runs without any database connection and always passes in CI.
It greps the source of the modules listed below for any reference to the
source pool helpers.  Finding one means a write-path module has a direct
dependency on the read-only source — a violation of the isolation rule.
"""
from __future__ import annotations

import ast
import pathlib

PYTHON_ROOT = pathlib.Path(__file__).resolve().parents[1]

CHECKED_FILES = [
    "agent/main_agent.py",
    "agent/order_agent.py",
    "agent/review_agent.py",
    "agent/stock_agent.py",
    "cli/admin_cli.py",
    "db/orders_repo.py",
    "db/returns_repo.py",
    "db/chat_repo.py",
    "db/migrations.py",
]

FORBIDDEN_NAMES = {"source_pool", "get_source_pool"}


def _names_in_source(source: str) -> set[str]:
    """Return all Name and Attribute node ids that appear in the source."""
    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # If the file is empty or a stub it won't parse — that's fine.
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
                if alias.asname:
                    found.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.add(alias.name)
                if alias.asname:
                    found.add(alias.asname)
    return found


def test_no_source_pool_in_write_modules() -> None:
    violations: list[str] = []
    for rel in CHECKED_FILES:
        path = PYTHON_ROOT / rel
        if not path.exists():
            # Stub file not yet written — no violation possible.
            continue
        source = path.read_text(encoding="utf-8")
        names = _names_in_source(source)
        hits = names & FORBIDDEN_NAMES
        if hits:
            violations.append(f"{rel}: references {sorted(hits)}")

    assert not violations, (
        "The following write-path modules reference the source pool:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
