"""AgentContext — single object passed to every tool factory.

The CLI creates one AgentContext per chat session and passes it into the
make_*_tools() factories via functools.partial.  The context object never
appears in any tool's JSON schema; it is always a private _ctx keyword arg.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from config import Settings
from db.catalog import CatalogSQL


@dataclass
class AgentContext:
    copy_pool: ConnectionPool
    catalog_sql: CatalogSQL
    session_id: str
    settings: Settings
