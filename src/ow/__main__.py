#!/usr/bin/env python3
import sys
from typing import Any, Optional

import typer

from ow.commands import (
    cmd_apply,
    cmd_init,
    cmd_prune,
    cmd_rebase,
    cmd_status,
)
from ow.utils.config import Config, load_global_config, parse_branch_spec
from ow.utils.templates import available_templates

try:
    from ow._version import version as __version__
except ImportError:
    __version__ = "dev"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ow {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="ow",
    help="Odoo workspace manager",
    no_args_is_help=True,
)


@app.callback()
def callback(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        "-v",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Odoo workspace manager."""
    pass


def _load_config() -> Config:
    """Load the user's global configuration, bootstrapping it on first use."""
    return load_global_config()


def _available_repo_aliases() -> list[str]:
    """Return repo aliases from the global config in declaration order."""
    try:
        cfg = load_global_config()
        return list(cfg.remotes.keys())
    except Exception:
        # Completion must never crash the shell, whatever state the config is in.
        return []


def _provided_repo_aliases(ctx: typer.Context) -> set[str]:
    """Aliases already given via -r on the command line being completed."""
    return {item.partition(":")[0] for item in (ctx.params.get("repo") or ())}


def _parse_repo_value(value: list[str] | None) -> dict[str, Any] | None:
    """Parse repo pairs from repeated -r ALIAS:SPEC options."""
    if not value:
        return None
    repo_pairs: dict[str, Any] = {}
    for item in value:
        alias, sep, spec = item.partition(":")
        if not sep or not alias or not spec:
            raise typer.BadParameter(
                f"--repo expects ALIAS:SPEC (got {item!r}), e.g. -r community:master..x"
            )
        repo_pairs[alias] = parse_branch_spec(spec)
    return repo_pairs


def complete_gen_templates(ctx: typer.Context, incomplete: str) -> list[str]:
    """Tab completion for -t/--template."""
    try:
        config = load_global_config()
        templates = available_templates(config)
    except Exception:
        # Completion must never crash the shell, whatever state the config is in.
        templates = []
    return [name for name in templates if name.startswith(incomplete)]


def complete_gen_repos(ctx: typer.Context, incomplete: str) -> list[str]:
    """Tab completion for -r/--repo — offers unused aliases."""
    provided = _provided_repo_aliases(ctx)
    return [
        alias for alias in _available_repo_aliases()
        if alias not in provided and alias.startswith(incomplete)
    ]


def complete_workspace_name(ctx: typer.Context, incomplete: str) -> list[str]:
    """Tab completion for workspace name.

    Disabled for now: workspaces are no longer confined to one project root,
    so there is no fixed directory to list from. Task 5 restores this via
    the discovery index.
    """
    return []


@app.command()
def init(
    name: Optional[str] = typer.Argument(None, help="Workspace directory to create under the current one (default: the current directory itself)"),
    configuration: Optional[str] = typer.Option(None, "--configuration", "-c", help="Path to existing workspace config to duplicate"),
    template: Optional[list[str]] = typer.Option(None, "--template", "-t", help="Templates to apply (repeatable)", autocompletion=complete_gen_templates),
    repo: Optional[list[str]] = typer.Option(None, "--repo", "-r", help="Repo alias and branch spec (repeatable, e.g. -r community:master..x)", autocompletion=complete_gen_repos),
) -> None:
    """Create a workspace here, or in ./NAME."""
    config = _load_config()
    cmd_init(config, name=name, templates=template, repos=_parse_repo_value(repo), configuration=configuration)


@app.command()
def apply(
    workspace: Optional[str] = typer.Argument(None, help="Workspace name (default: resolve from cwd)", autocompletion=complete_workspace_name),
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated repo aliases to materialize (default: all)"),
) -> None:
    """Re-render templates and materialize worktrees."""
    config = _load_config()
    cmd_apply(config, workspace=workspace, only=only)


@app.command()
def status(
    workspace: Optional[str] = typer.Argument(None, help="Workspace name (default: resolve from cwd)", autocompletion=complete_workspace_name),
) -> None:
    """Show workspace status."""
    config = _load_config()
    cmd_status(config, workspace=workspace)


@app.command()
def rebase(
    workspace: Optional[str] = typer.Argument(None, help="Workspace name (default: resolve from cwd)", autocompletion=complete_workspace_name),
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated repo aliases to rebase (default: all)"),
    autostash: bool = typer.Option(False, "--autostash", help="Stash and restore uncommitted changes around each rebase"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the git commands without running them"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Fetch and rebase workspace branches."""
    config = _load_config()
    cmd_rebase(
        config, workspace=workspace, only=only,
        autostash=autostash, dry_run=dry_run, yes=yes,
    )


@app.command()
def prune() -> None:
    """Clean up stale worktree references and orphaned branches."""
    config = _load_config()
    cmd_prune(config)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        # 130 is the conventional shell status for SIGINT. parallel_per_repo
        # has already killed the git children by the time we get here.
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
