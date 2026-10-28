"""Typer entrypoint: python main.py <command>."""
from __future__ import annotations

import typer

from cli.admin_cli import admin_app

app = typer.Typer(
    name="shopping-agent",
    help="Configurable AI shopping/support agent (terminal backend).",
    no_args_is_help=True,
)

app.add_typer(admin_app, name="admin", help="Local admin commands (no keys needed).")


@app.callback()
def _root() -> None:
    """Run `python main.py --help` to see all commands."""


def _register() -> None:
    # imported here so --help stays fast and import errors surface nicely
    from cli.check_cli import check
    from cli.chat_cli import chat
    from cli.setup_cli import clone_command, confirm_mapping, diff_command, \
        migrate_command, reset_copy_command, scan_command

    app.command("check", help="Validate env, connections, Groq key and setup state.")(check)
    app.command("scan", help="Inspect the source DB and propose a mapping.")(scan_command)
    app.command("confirm-mapping",
                help="Walk through the proposal and save the confirmed mapping.")(
        confirm_mapping)
    app.command("clone", help="Clone the source catalog into the copy schema.")(
        clone_command)
    app.command("migrate", help="Create record tables (orders/returns/refunds) in "
                                "the copy schema.")(migrate_command)
    app.command("diff", help="Compare source vs copy (stock, orders, returns).")(
        diff_command)
    app.command("reset-copy", help="Drop and recreate the copy schema "
                                   "(type its name to confirm).")(reset_copy_command)
    app.command("chat", help="Start the terminal chat agent.")(chat)


_register()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
