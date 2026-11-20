"""CLI application builder for ow.

Every Typer signature lives here (a `commands/` module never imports `ow.cli`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click
import typer
from typer.core import TyperGroup

from . import __version__, errors, term

if TYPE_CHECKING:
    from ow.core.plugins import Plugins

# Registers the vendored click's shell-completion classes, which `shell-init` reads.
typer.completion.completion_init()


class OwGroup(TyperGroup):
    """Custom Typer group that handles no_args_is_help with exit 0 and OwError exceptions."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args and not ctx.resilient_parsing:
            term.out(ctx.get_help())
            ctx.exit(0)
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> None:
        try:
            super().invoke(ctx)
        except errors.OwError as exc:
            raise typer.Exit(fail(exc)) from None


def fail(exc: errors.OwError) -> int:
    """Print an expected failure and return its exit code: the one OwError handler."""
    term.error(str(exc))
    return exc.exit_code


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
        """The ow root group: nothing of its own beyond --version."""

    plugins.add_commands(app)
    return app
