"""`python main.py check` - environment, connections, Groq and setup state."""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

import config
import db.pools as pools
from config import ConfigError, mask_key, mask_url

console = Console()

EXPECTED_MODEL = "llama-3.3-70b-versatile"


def check(groq: bool = typer.Option(False, "--groq", help="Also verify the Groq key/model online.")) -> None:
    """Validate env, test both connections, Groq key, state of setup."""
    failures = 0

    def ok(text: str) -> None:
        console.print(f"  [green]ok[/green]  {text}")

    def bad(text: str) -> None:
        nonlocal failures
        failures += 1
        console.print(f"  [red]FAIL[/red] {text}")

    def warn(text: str) -> None:
        console.print(f"  [yellow]warn[/yellow] {text}")

    console.print("[bold]1. Environment[/bold]")
    try:
        settings = config.load_settings()
        pools.close_pools()  # reload with fresh env
        ok(f"SOURCE_DATABASE_URL = {mask_url(settings.source_database_url)}")
        ok(f"COPY_DATABASE_URL = {mask_url(settings.copy_database_url) or '(same database)'}")
        ok(f"Schemas: source={settings.source_schema} copy={settings.copy_schema}")
        ok(f"Model={settings.groq_model} Store={settings.store_name} "
           f"Currency={settings.currency} ReturnWindow={settings.return_window_days}d "
           f"TTL={settings.confirmation_ttl_minutes}min")
        if settings.groq_api_key:
            ok(f"GROQ_API_KEY present ({mask_key(settings.groq_api_key)})")
        else:
            warn("GROQ_API_KEY not set - needed for `chat` (and `scan --use-llm`).")
    except ConfigError as exc:
        bad(str(exc))
        raise typer.Exit(1)

    console.print("[bold]2. Source connection (READ ONLY)[/bold]")
    try:
        pool = pools.get_source_pool()
        with pool.connection() as conn:
            ro = conn.execute("SHOW transaction_read_only").fetchone()
            ver = conn.execute("SELECT version() AS v").fetchone()["v"]
        if ro["transaction_read_only"] == "on":
            ok(f"connected; transactions are READ ONLY ({ver.split(',')[0]})")
        else:
            bad("source connection is NOT read only - refusing to continue")
    except Exception as exc:
        bad(f"cannot connect to source: {exc.__class__.__name__}: {exc}")

    console.print("[bold]3. Copy connection[/bold]")
    try:
        pool = pools.get_copy_pool()
        with pool.connection() as conn:
            ver = conn.execute("SELECT current_database() AS db, version() AS v").fetchone()
        ok(f"connected to {ver['db']} ({ver['v'].split(',')[0]})")
    except Exception as exc:
        bad(f"cannot connect to copy database: {exc.__class__.__name__}: {exc}")

    console.print("[bold]4. Groq[/bold]")
    if not settings.groq_key_present:
        warn("GROQ_API_KEY not set (only required for chat).")
    elif groq:
        try:
            from groq import Groq
            client = Groq(api_key=settings.groq_api_key)
            ids = [m.id for m in client.models.list().data]
            if settings.groq_model in ids:
                ok(f"key valid; model {settings.groq_model} available")
            else:
                bad(f"model {settings.groq_model} NOT in Groq's model list "
                    f"({len(ids)} models found). Pick one from console.groq.com.")
        except Exception as exc:
            bad(f"Groq verification failed: {exc.__class__.__name__}")
    else:
        warn(f"key present but not verified online (use --groq; default model "
             f"{EXPECTED_MODEL} is on Groq's tool-calling list)")

    console.print("[bold]5. Setup state[/bold]")
    from db.scanner import MAPPING_PATH, load_mapping
    if MAPPING_PATH.exists():
        try:
            mapping = load_mapping(require_confirmed=False)
            state = "confirmed" if mapping.get("confirmed_at") else "NOT confirmed"
            ok(f"mapping.json found ({state}); products table = "
               f"{mapping.get('products', {}).get('table')}")
        except Exception as exc:
            bad(f"mapping.json invalid: {exc}")
    else:
        warn("no mapping yet - run `python main.py scan` then `confirm-mapping`")

    try:
        copy_pool = pools.get_copy_pool()
        from psycopg import sql
        with copy_pool.connection() as conn:
            n = conn.execute(
                sql.SQL("SELECT count(*) AS n FROM pg_class c "
                        "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
                        "WHERE ns.nspname = %s AND c.relkind = 'r'"),
                (settings.copy_schema,)).fetchone()["n"]
        if n > 0:
            ok(f"copy schema has {n} table(s)")
            from db.migrations import record_tables_applied
            if record_tables_applied(pools.get_copy_pool()):
                ok("record tables present (run `chat` any time)")
            else:
                warn("record tables missing - run `python main.py migrate`")
        else:
            warn("copy schema empty - run `python main.py clone`")
    except Exception as exc:
        warn(f"copy schema check skipped: {exc.__class__.__name__}")

    pools.close_pools()
    if failures:
        console.print(f"[bold red]{failures} check(s) failed.[/bold red]")
        raise typer.Exit(1)
    console.print("[bold green]All checks passed.[/bold green]")
