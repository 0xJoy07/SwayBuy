"""Chat message persistence."""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql


def _s(schema: str) -> sql.Identifier:
    return sql.Identifier(schema)


def save_message(
    conn: psycopg.Connection,
    copy_schema: str,
    session_id: str,
    role: str,
    content: str,
) -> None:
    """Persist one chat turn to chat_messages."""
    conn.execute(
        sql.SQL(
            "INSERT INTO {s}.chat_messages (session_id, role, content) "
            "VALUES (%s, %s, %s)"
        ).format(s=_s(copy_schema)),
        (session_id, role, content),
    )


def get_history(
    conn: psycopg.Connection,
    copy_schema: str,
    session_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent `limit` messages for this session, oldest first."""
    rows = conn.execute(
        sql.SQL(
            "SELECT role, content, created_at FROM ("
            "  SELECT role, content, created_at "
            "  FROM {s}.chat_messages "
            "  WHERE session_id = %s "
            "  ORDER BY created_at DESC LIMIT %s"
            ") sub ORDER BY created_at ASC"
        ).format(s=_s(copy_schema)),
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
