"""Clone source catalog tables into the copy schema.

Rules enforced here:
- Writes go only to the copy pool. The source pool is read-only.
- All identifiers use psycopg.sql.Identifier — never string-formatted.
- Sequences in the copy schema are independent of the source schema.
- Views, functions, triggers, and RLS are skipped (listed in output).
- Clone metadata is saved in a _clone_meta table in the copy schema.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def clone(
    source_pool: ConnectionPool,
    copy_pool: ConnectionPool,
    settings: Any,
    mapping: dict[str, Any],
    only_mapped: bool = False,
) -> None:
    """Clone source tables into the copy schema.

    Aborts if the copy schema already has tables — tell user to run reset-copy.
    """
    src_schema = settings.source_schema
    cpy_schema = settings.copy_schema

    with copy_pool.connection() as cpy_conn:
        existing = _list_tables(cpy_conn, cpy_schema)
        non_meta = [t for t in existing if t != "_clone_meta"]
        if non_meta:
            console.print(
                f"[red]Copy schema [cyan]{cpy_schema}[/cyan] already has "
                f"{len(non_meta)} table(s): {', '.join(non_meta[:5])}.\n"
                "Run [bold]python main.py reset-copy[/bold] first.[/red]"
            )
            return

        cpy_conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(cpy_schema)
            )
        )

    with source_pool.connection() as src_conn:
        src_conn.execute("BEGIN READ ONLY")
        src_tables = _list_tables(src_conn, src_schema)
        src_conn.execute("ROLLBACK")

    if only_mapped:
        mapped_tables = _mapped_table_names(mapping)
        src_tables = [t for t in src_tables if t in mapped_tables]

    skipped: list[str] = []
    cloned: list[str] = []
    row_counts: list[tuple[str, int, int]] = []

    same_db = settings.same_database

    for tname in src_tables:
        console.print(f"  Cloning [cyan]{tname}[/cyan]…")
        try:
            with copy_pool.connection() as cpy_conn:
                _create_table_like(cpy_conn, src_schema, cpy_schema, tname, source_pool)
                _fix_sequences(cpy_conn, src_schema, cpy_schema, tname, source_pool)

            if same_db:
                src_count, cpy_count = _copy_rows_same_db(
                    copy_pool, src_schema, cpy_schema, tname
                )
            else:
                src_count, cpy_count = _copy_rows_cross_db(
                    source_pool, copy_pool, src_schema, cpy_schema, tname
                )

            row_counts.append((tname, src_count, cpy_count))
            cloned.append(tname)
        except Exception as exc:
            skipped.append(f"{tname}: {exc.__class__.__name__}: {exc}")

    # Recreate foreign keys
    with source_pool.connection() as src_conn:
        src_conn.execute("BEGIN READ ONLY")
        fks = _read_foreign_keys(src_conn, src_schema, src_tables)
        src_conn.execute("ROLLBACK")

    with copy_pool.connection() as cpy_conn:
        for fk in fks:
            if fk["table_name"] not in cloned or fk["foreign_table"] not in cloned:
                skipped.append(
                    f"FK {fk['constraint_name']}: referenced table not cloned"
                )
                continue
            try:
                _recreate_fk(cpy_conn, cpy_schema, fk)
            except Exception as exc:
                skipped.append(
                    f"FK {fk['constraint_name']}: {exc.__class__.__name__}: {exc}"
                )

    _save_clone_meta(copy_pool, cpy_schema, cloned, row_counts)
    _print_clone_summary(row_counts, skipped)


def diff(
    source_pool: ConnectionPool,
    copy_pool: ConnectionPool,
    settings: Any,
    mapping: dict[str, Any],
) -> None:
    """Print per-product stock deltas and order/return/refund counts."""
    src_schema = settings.source_schema
    cpy_schema = settings.copy_schema
    prod = mapping.get("products", {})
    prod_table = prod.get("table")
    stock_col = prod.get("stock_col")
    name_col = prod.get("name_col")
    id_col = prod.get("id_col")

    if not all([prod_table, stock_col, name_col, id_col]):
        console.print("[red]Mapping incomplete — cannot diff.[/red]")
        return

    t = Table(title="Stock diff: source vs copy", show_lines=True)
    t.add_column("Product", style="cyan")
    t.add_column("Source stock", justify="right")
    t.add_column("Copy stock", justify="right")
    t.add_column("Delta", justify="right")

    with source_pool.connection() as src_conn:
        src_conn.execute("BEGIN READ ONLY")
        src_rows = src_conn.execute(
            sql.SQL("SELECT {id}, {name}, {stock} FROM {schema}.{table} ORDER BY {id}").format(
                id=sql.Identifier(id_col),
                name=sql.Identifier(name_col),
                stock=sql.Identifier(stock_col),
                schema=sql.Identifier(src_schema),
                table=sql.Identifier(prod_table),
            )
        ).fetchall()
        src_conn.execute("ROLLBACK")

    with copy_pool.connection() as cpy_conn:
        cpy_rows = cpy_conn.execute(
            sql.SQL("SELECT {id}, {stock} FROM {schema}.{table} ORDER BY {id}").format(
                id=sql.Identifier(id_col),
                stock=sql.Identifier(stock_col),
                schema=sql.Identifier(cpy_schema),
                table=sql.Identifier(prod_table),
            )
        ).fetchall()

    cpy_stock = {r[id_col]: r[stock_col] for r in cpy_rows}
    for sr in src_rows:
        pid = sr[id_col]
        src_s = sr[stock_col]
        cpy_s = cpy_stock.get(pid, "—")
        delta = (cpy_s - src_s) if isinstance(cpy_s, (int, float)) else "—"
        color = "green" if delta == 0 else ("red" if isinstance(delta, (int, float)) and delta < 0 else "yellow")
        t.add_row(
            str(sr[name_col]),
            str(src_s),
            str(cpy_s),
            f"[{color}]{delta}[/{color}]",
        )

    console.print(t)

    with copy_pool.connection() as cpy_conn:
        tables = _list_tables(cpy_conn, cpy_schema)
        for record_table in ("orders", "return_requests", "refunds"):
            if record_table in tables:
                n = cpy_conn.execute(
                    sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                        sql.Identifier(cpy_schema),
                        sql.Identifier(record_table),
                    )
                ).fetchone()["n"]
                console.print(f"  {record_table}: [cyan]{n}[/cyan] row(s)")


def reset_copy(copy_pool: ConnectionPool, settings: Any) -> None:
    """Drop the copy schema after the user types its name to confirm."""
    import typer

    cpy_schema = settings.copy_schema
    console.print(
        f"[bold red]This will DROP SCHEMA {cpy_schema!r} CASCADE — "
        "all data will be lost.[/bold red]"
    )
    typed = typer.prompt(f"Type the schema name to confirm").strip()
    if typed != cpy_schema:
        console.print("[yellow]Aborted — schema name did not match.[/yellow]")
        return

    with copy_pool.connection() as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(cpy_schema)
            )
        )
    console.print(f"[green]Schema {cpy_schema!r} dropped.[/green]")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_tables(conn: psycopg.Connection, schema: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    ).fetchall()
    return [r["table_name"] for r in rows]


def _mapped_table_names(mapping: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    prod = mapping.get("products", {})
    if prod.get("table"):
        names.add(prod["table"])
    reviews = mapping.get("reviews", {})
    if reviews and reviews.get("table"):
        names.add(reviews["table"])
    return names


def _create_table_like(
    cpy_conn: psycopg.Connection,
    src_schema: str,
    cpy_schema: str,
    tname: str,
    source_pool: ConnectionPool,
) -> None:
    """CREATE TABLE … LIKE in copy schema, including defaults, constraints, indexes."""
    cpy_conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {cpy}.{t} "
            "(LIKE {src}.{t} INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
            "INCLUDING INDEXES)"
        ).format(
            cpy=sql.Identifier(cpy_schema),
            src=sql.Identifier(src_schema),
            t=sql.Identifier(tname),
        )
    )


def _fix_sequences(
    cpy_conn: psycopg.Connection,
    src_schema: str,
    cpy_schema: str,
    tname: str,
    source_pool: ConnectionPool,
) -> None:
    """Recreate sequences for serial/identity columns in the copy schema.

    The new sequences are fully independent of the source schema.
    """
    # Find columns that reference a sequence (serial / bigserial / identity)
    with source_pool.connection() as src_conn:
        src_conn.execute("BEGIN READ ONLY")
        seq_cols = src_conn.execute(
            """
            SELECT a.attname                         AS col,
                   pg_get_expr(ad.adbin, ad.adrelid) AS def_expr
            FROM pg_attribute      a
            JOIN pg_attrdef        ad ON ad.adrelid = a.attrelid
                                     AND ad.adnum   = a.attnum
            JOIN pg_class          c  ON c.oid      = a.attrelid
            JOIN pg_namespace      ns ON ns.oid      = c.relnamespace
            WHERE ns.nspname = %s
              AND c.relname  = %s
              AND a.attnum   > 0
              AND NOT a.attisdropped
              AND pg_get_expr(ad.adbin, ad.adrelid) ILIKE '%%nextval%%'
            """,
            (src_schema, tname),
        ).fetchall()

        max_vals: dict[str, int] = {}
        for row in seq_cols:
            col = row["col"]
            try:
                mv = src_conn.execute(
                    sql.SQL("SELECT COALESCE(MAX({}), 0) AS m FROM {}.{}").format(
                        sql.Identifier(col),
                        sql.Identifier(src_schema),
                        sql.Identifier(tname),
                    )
                ).fetchone()["m"]
                max_vals[col] = int(mv)
            except Exception:
                max_vals[col] = 0
        src_conn.execute("ROLLBACK")

    for row in seq_cols:
        col = row["col"]
        seq_name = f"{tname}_{col}_seq"
        start = max_vals.get(col, 0) + 1

        cpy_conn.execute(
            sql.SQL("DROP SEQUENCE IF EXISTS {}.{}").format(
                sql.Identifier(cpy_schema),
                sql.Identifier(seq_name),
            )
        )
        cpy_conn.execute(
            sql.SQL(
                "CREATE SEQUENCE {cpy}.{seq} START WITH %s INCREMENT BY 1 NO CYCLE"
            ).format(
                cpy=sql.Identifier(cpy_schema),
                seq=sql.Identifier(seq_name),
            ),
            (start,),
        )
        cpy_conn.execute(
            sql.SQL(
                "ALTER TABLE {cpy}.{t} ALTER COLUMN {col} "
                "SET DEFAULT nextval('{cpy_literal}.{seq_literal}')"
            ).format(
                cpy=sql.Identifier(cpy_schema),
                t=sql.Identifier(tname),
                col=sql.Identifier(col),
                cpy_literal=sql.Literal(cpy_schema),
                seq_literal=sql.Literal(seq_name),
            )
        )


def _copy_rows_same_db(
    copy_pool: ConnectionPool,
    src_schema: str,
    cpy_schema: str,
    tname: str,
) -> tuple[int, int]:
    """INSERT INTO copy SELECT * FROM source — works when both schemas are in the same DB."""
    with copy_pool.connection() as conn:
        src_count = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(src_schema),
                sql.Identifier(tname),
            )
        ).fetchone()["n"]

        conn.execute(
            sql.SQL("INSERT INTO {cpy}.{t} SELECT * FROM {src}.{t}").format(
                cpy=sql.Identifier(cpy_schema),
                src=sql.Identifier(src_schema),
                t=sql.Identifier(tname),
            )
        )

        cpy_count = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(cpy_schema),
                sql.Identifier(tname),
            )
        ).fetchone()["n"]

    return int(src_count), int(cpy_count)


def _copy_rows_cross_db(
    source_pool: ConnectionPool,
    copy_pool: ConnectionPool,
    src_schema: str,
    cpy_schema: str,
    tname: str,
) -> tuple[int, int]:
    """COPY to buffer on source / COPY from buffer on copy — cross-database path."""
    buf = io.BytesIO()

    with source_pool.connection() as src_conn:
        src_conn.execute("BEGIN READ ONLY")
        src_count = src_conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(src_schema),
                sql.Identifier(tname),
            )
        ).fetchone()["n"]
        with src_conn.cursor() as cur:
            with cur.copy(
                sql.SQL("COPY {}.{} TO STDOUT (FORMAT binary)").format(
                    sql.Identifier(src_schema),
                    sql.Identifier(tname),
                )
            ) as copy_out:
                for chunk in copy_out:
                    buf.write(chunk)
        src_conn.execute("ROLLBACK")

    buf.seek(0)
    with copy_pool.connection() as cpy_conn:
        with cpy_conn.cursor() as cur:
            with cur.copy(
                sql.SQL("COPY {}.{} FROM STDIN (FORMAT binary)").format(
                    sql.Identifier(cpy_schema),
                    sql.Identifier(tname),
                )
            ) as copy_in:
                while True:
                    chunk = buf.read(65536)
                    if not chunk:
                        break
                    copy_in.write(chunk)
        cpy_count = cpy_conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(cpy_schema),
                sql.Identifier(tname),
            )
        ).fetchone()["n"]

    return int(src_count), int(cpy_count)


def _read_foreign_keys(
    src_conn: psycopg.Connection,
    schema: str,
    table_names: list[str],
) -> list[dict[str, str]]:
    if not table_names:
        return []
    rows = src_conn.execute(
        """
        SELECT tc.table_name,
               tc.constraint_name,
               kcu.column_name,
               ccu.table_name  AS foreign_table,
               ccu.column_name AS foreign_column,
               rc.update_rule,
               rc.delete_rule
        FROM information_schema.table_constraints  tc
        JOIN information_schema.key_column_usage   kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.table_schema    = tc.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        JOIN information_schema.referential_constraints rc
          ON rc.constraint_name  = tc.constraint_name
         AND rc.constraint_schema = tc.table_schema
        WHERE tc.table_schema    = %s
          AND tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_name      = ANY(%s)
        ORDER BY tc.table_name, tc.constraint_name
        """,
        (schema, table_names),
    ).fetchall()
    return [dict(r) for r in rows]


def _recreate_fk(
    cpy_conn: psycopg.Connection,
    cpy_schema: str,
    fk: dict[str, str],
) -> None:
    cpy_conn.execute(
        sql.SQL(
            "ALTER TABLE {cpy}.{t} ADD CONSTRAINT {con} "
            "FOREIGN KEY ({col}) REFERENCES {cpy}.{ft} ({fc}) "
            "ON UPDATE {on_upd} ON DELETE {on_del}"
        ).format(
            cpy=sql.Identifier(cpy_schema),
            t=sql.Identifier(fk["table_name"]),
            con=sql.Identifier(fk["constraint_name"]),
            col=sql.Identifier(fk["column_name"]),
            ft=sql.Identifier(fk["foreign_table"]),
            fc=sql.Identifier(fk["foreign_column"]),
            on_upd=sql.SQL(fk["update_rule"]),
            on_del=sql.SQL(fk["delete_rule"]),
        )
    )


def _save_clone_meta(
    copy_pool: ConnectionPool,
    cpy_schema: str,
    cloned: list[str],
    row_counts: list[tuple[str, int, int]],
) -> None:
    with copy_pool.connection() as conn:
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.{} ("
                "  cloned_at     timestamptz NOT NULL DEFAULT now(), "
                "  tables_cloned jsonb       NOT NULL, "
                "  row_counts    jsonb        NOT NULL"
                ")"
            ).format(
                sql.Identifier(cpy_schema),
                sql.Identifier("_clone_meta"),
            )
        )
        conn.execute(
            sql.SQL("INSERT INTO {}.{} (cloned_at, tables_cloned, row_counts) "
                    "VALUES (now(), %s, %s)").format(
                sql.Identifier(cpy_schema),
                sql.Identifier("_clone_meta"),
            ),
            (
                json.dumps(cloned),
                json.dumps({t: {"src": s, "cpy": c} for t, s, c in row_counts}),
            ),
        )


def _print_clone_summary(
    row_counts: list[tuple[str, int, int]],
    skipped: list[str],
) -> None:
    t = Table(title="Clone row-count verification", show_lines=True)
    t.add_column("Table", style="cyan")
    t.add_column("Source rows", justify="right")
    t.add_column("Copy rows", justify="right")
    t.add_column("Match")

    for tname, src, cpy in row_counts:
        match = "[green]✓[/green]" if src == cpy else "[red]✗[/red]"
        t.add_row(tname, str(src), str(cpy), match)

    console.print(t)

    if skipped:
        console.print("[yellow]Skipped items:[/yellow]")
        for s in skipped:
            console.print(f"  [yellow]•[/yellow] {s}")
