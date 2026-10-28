"""Setup sub-commands: scan, confirm-mapping, clone, migrate, diff, reset-copy."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def scan_command(
    use_llm: bool = typer.Option(False, "--use-llm",
                                 help="Use the Groq LLM to refine the mapping proposal."),
) -> None:
    """Inspect the source database and write a proposed mapping to state/."""
    import config
    import db.pools as pools
    from db.scanner import PROPOSED_PATH, introspect_schema, propose_mapping

    settings = config.load_settings(
        require_groq=use_llm,
        require_source=True,
    )

    console.print("[bold]Introspecting source schema…[/bold]")
    src_pool = pools.get_source_pool()
    schema_info = introspect_schema(src_pool, settings.source_schema)
    pools.close_pools()

    tables = schema_info.get("tables", {})
    console.print(f"  Found [cyan]{len(tables)}[/cyan] table(s) in schema "
                  f"[cyan]{settings.source_schema!r}[/cyan]")

    console.print("[bold]Proposing mapping…[/bold]")
    proposal = propose_mapping(
        schema_info,
        use_llm=use_llm,
        groq_api_key=settings.groq_api_key if use_llm else None,
        groq_model=settings.groq_model,
    )

    PROPOSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSED_PATH.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    console.print(f"  Proposal written to [cyan]{PROPOSED_PATH}[/cyan]")

    needs = proposal.get("needs_review", [])
    if needs:
        console.print(
            Panel(
                "\n".join(f"  • {n}" for n in needs),
                title="[yellow]Needs manual review[/yellow]",
                border_style="yellow",
            )
        )

    _print_proposal_table(proposal)
    console.print(
        "\nRun [bold]python main.py confirm-mapping[/bold] to review and save."
    )


def _print_proposal_table(proposal: dict) -> None:
    prod = proposal.get("products", {})
    t = Table(title="Proposed mapping — products", show_lines=True)
    t.add_column("Role", style="cyan")
    t.add_column("Mapped to")
    for role in ("table", "id_col", "name_col", "price_col", "stock_col", "category_col"):
        val = prod.get(role) or "[dim]—[/dim]"
        t.add_row(role, str(val))
    console.print(t)

    reviews = proposal.get("reviews")
    if reviews:
        r = Table(title="Proposed mapping — reviews", show_lines=True)
        r.add_column("Role", style="cyan")
        r.add_column("Mapped to")
        for role in ("table", "review_id_col", "product_ref_col",
                     "rating_col", "review_text_col"):
            val = reviews.get(role) or "[dim]—[/dim]"
            r.add_row(role, str(val))
        console.print(r)


# ---------------------------------------------------------------------------
# confirm-mapping
# ---------------------------------------------------------------------------

def confirm_mapping() -> None:
    """Walk through the proposed mapping, prompt for any corrections, and save."""
    import config
    import db.pools as pools
    from db.scanner import (
        MAPPING_PATH,
        PROPOSED_PATH,
        MappingError,
        introspect_schema,
        validate_mapping,
    )

    if not PROPOSED_PATH.exists():
        console.print(
            "[red]No mapping.proposed.json found. "
            "Run `python main.py scan` first.[/red]"
        )
        raise typer.Exit(1)

    proposal: dict = json.loads(PROPOSED_PATH.read_text(encoding="utf-8"))
    _print_proposal_table(proposal)

    console.print("\n[bold]Confirm or override each field.[/bold] "
                  "Press Enter to accept the current value.\n")

    prod = proposal.setdefault("products", {})
    for role in ("table", "id_col", "name_col", "price_col", "stock_col"):
        current = prod.get(role) or ""
        answer = typer.prompt(
            f"  products.{role}",
            default=current,
            show_default=True,
        ).strip()
        if answer:
            prod[role] = answer

    optional_role = "category_col"
    current = prod.get(optional_role) or ""
    answer = typer.prompt(
        f"  products.{optional_role} (optional)",
        default=current or "skip",
        show_default=True,
    ).strip()
    if answer and answer.lower() != "skip":
        prod[optional_role] = answer
    elif answer.lower() == "skip":
        prod.pop(optional_role, None)

    reviews = proposal.get("reviews")
    if reviews is not None:
        include_reviews = typer.confirm(
            "  Include reviews mapping?", default=True
        )
        if include_reviews:
            rtable = reviews.get("table") or ""
            answer = typer.prompt(
                "  reviews.table", default=rtable, show_default=True
            ).strip()
            if answer:
                reviews["table"] = answer
            for role in ("review_id_col", "product_ref_col",
                         "rating_col", "review_text_col"):
                current = reviews.get(role) or ""
                answer = typer.prompt(
                    f"  reviews.{role} (optional, Enter to skip)",
                    default=current or "skip",
                    show_default=True,
                ).strip()
                if answer and answer.lower() != "skip":
                    reviews[role] = answer
                else:
                    reviews.pop(role, None)
        else:
            proposal.pop("reviews", None)

    # Validate — requires a live source connection
    console.print("\n[bold]Validating mapping against source schema…[/bold]")
    try:
        settings = config.load_settings(require_source=True)
        src_pool = pools.get_source_pool()
        from db.scanner import introspect_schema
        schema_info = introspect_schema(src_pool, settings.source_schema)
        pools.close_pools()
        errors = validate_mapping(proposal, schema_info)
        if errors:
            console.print("[red]Validation errors:[/red]")
            for err in errors:
                console.print(f"  [red]•[/red] {err}")
            retry = typer.confirm("Save anyway?", default=False)
            if not retry:
                raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[yellow]Could not validate online ({exc.__class__.__name__}: {exc}). "
                      "Saving without DB validation.[/yellow]")

    proposal["confirmed_at"] = datetime.now(tz=timezone.utc).isoformat()
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_PATH.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    console.print(
        f"\n[green]mapping.json saved to {MAPPING_PATH}[/green]"
    )


# ---------------------------------------------------------------------------
# clone (stub — implemented in Task 3)
# ---------------------------------------------------------------------------

def clone_command(
    only_mapped: bool = typer.Option(
        False, "--only-mapped",
        help="Clone only the tables referenced in the confirmed mapping."
    ),
) -> None:
    """Clone the source catalog tables into the copy schema."""
    import config
    import db.pools as pools
    from db.cloner import clone
    from db.scanner import load_mapping

    settings = config.load_settings(require_source=True)
    mapping = load_mapping(require_confirmed=True)
    src_pool = pools.get_source_pool()
    cpy_pool = pools.get_copy_pool()
    clone(src_pool, cpy_pool, settings, mapping, only_mapped=only_mapped)
    pools.close_pools()


# ---------------------------------------------------------------------------
# migrate (stub — implemented in Task 4)
# ---------------------------------------------------------------------------

def migrate_command() -> None:
    """Create record tables (orders, returns, refunds…) in the copy schema."""
    import config
    import db.pools as pools
    from db.migrations import migrate

    settings = config.load_settings(require_source=False)
    cpy_pool = pools.get_copy_pool()
    migrate(cpy_pool, settings.copy_schema)
    pools.close_pools()
    console.print("[green]All record tables created.[/green]")


# ---------------------------------------------------------------------------
# diff (stub — implemented in Task 3)
# ---------------------------------------------------------------------------

def diff_command() -> None:
    """Compare source vs copy: stock deltas, order/return/refund counts."""
    import config
    import db.pools as pools
    from db.cloner import diff
    from db.scanner import load_mapping

    settings = config.load_settings(require_source=True)
    mapping = load_mapping(require_confirmed=True)
    src_pool = pools.get_source_pool()
    cpy_pool = pools.get_copy_pool()
    diff(src_pool, cpy_pool, settings, mapping)
    pools.close_pools()


# ---------------------------------------------------------------------------
# reset-copy (stub — implemented in Task 3)
# ---------------------------------------------------------------------------

def reset_copy_command() -> None:
    """Drop and recreate the copy schema (destructive — type the schema name to confirm)."""
    import config
    import db.pools as pools
    from db.cloner import reset_copy

    settings = config.load_settings(require_source=False)
    cpy_pool = pools.get_copy_pool()
    reset_copy(cpy_pool, settings)
    pools.close_pools()
