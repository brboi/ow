"""CLI application builder for ow."""
from __future__ import annotations

from typing import TYPE_CHECKING

import click
import typer
from typer.core import TyperGroup

from . import __version__
from . import errors, term

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


class OwGroup(TyperGroup):
    """Custom Typer group that handles no_args_is_help with exit 0 and OwError exceptions."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Override to handle bare 'ow' (no args) by printing help and exiting with 0."""
        if not args:
            term.out(ctx.get_help())
            ctx.exit(0)
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> None:
        """Override to catch OwError, print it, and exit with its code."""
        try:
            super().invoke(ctx)
        except errors.OwError as exc:
            term.error(str(exc))
            raise typer.Exit(exc.exit_code) from None

def build_app(plugins: Plugins) -> typer.Typer:
    """Build the Typer application with plugins."""
    
    def version_callback(value: bool) -> None:
        if value:
            term.out(f"ow {__version__}")
            raise typer.Exit()
    
    app = typer.Typer(
        cls=OwGroup,
        help="Ready-to-code Odoo workspaces on git worktrees",
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
    )
    
    @app.callback()
    def app_callback(
        version: bool = typer.Option(
            None,
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit",
        ),
    ) -> None:
        """Callback to keep app as a group even with no commands yet."""
        pass
    
    # Add plugin commands
    plugins.add_commands(app)
    
    return app
