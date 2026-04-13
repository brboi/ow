#!/usr/bin/env python3
from pathlib import Path
from typing import Any, Optional

import typer
from click.shell_completion import CompletionItem

from ow.commands import (
    cmd_create,
    cmd_init,
    cmd_prune,
    cmd_rebase,
    cmd_status,
    cmd_update,
)
from ow.utils.config import Config, load_config, parse_branch_spec
from ow.utils.templates import available_templates

try:
    from ow._version import version as __version__
except ImportError:
    __version__ = "dev"

app = typer.Typer(
    name="ow",
    help="Odoo workspace manager",
    no_args_is_help=True,
)


def _find_root() -> Path:
    current = Path.cwd()
    while True:
        if (current / "ow.toml").exists() or (current / "ow.toml.example").exists():
            return current
        if current.parent == current:
            raise FileNotFoundError(
                "ow.toml not found in current directory or any parent"
            )
        current = current.parent


def _load_config() -> Config:
    """Find root and load ow.toml, creating minimal config if needed."""
    try:
        root = _find_root()
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    toml_path = root / "ow.toml"
    if not toml_path.exists():
        minimal_config = """\
[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"
"""
        toml_path.write_text(minimal_config)
        typer.echo("Created ow.toml with default configuration. Edit it to suit your needs.")

    return load_config(toml_path)


def _available_repo_aliases() -> list[str]:
    """Return repo aliases from ow.toml in declaration order."""
    try:
        root = _find_root()
        toml_path = root / "ow.toml"
        if toml_path.exists():
            cfg = load_config(toml_path)
            return list(cfg.remotes.keys())
    except (FileNotFoundError, Exception):
        pass
    return []


def _parse_repo_args(args: list[str]) -> set[str]:
    """Extract already-provided repo aliases from CLI args."""
    provided = set()
    i = 0
    while i < len(args):
        if args[i] in ("-r", "--repo") and i + 1 < len(args):
            alias = args[i + 1].split(":")[0]
            provided.add(alias)
            i += 2
        else:
            i += 1
    return provided


def _parse_repo_value(value: list[str] | None) -> dict[str, Any] | None:
    """Parse repo pairs from repeated -r ALIAS:SPEC options."""
    if not value:
        return None
    repo_pairs: dict[str, Any] = {}
    for item in value:
        if ":" in item:
            alias, spec = item.split(":", 1)
            repo_pairs[alias] = parse_branch_spec(spec)
    return repo_pairs


def complete_gen_templates(ctx: typer.Context, incomplete: str) -> list[CompletionItem]:
    """Tab completion for -t/--template."""
    try:
        root = _find_root()
        config = Config(vars={}, remotes={}, root_dir=root)
        templates = available_templates(config)
    except (FileNotFoundError, Exception):
        templates = []
    return [CompletionItem(name) for name in templates if name.startswith(incomplete)]


def complete_gen_repos(ctx: typer.Context, incomplete: str) -> list[CompletionItem]:
    """Tab completion for -r/--repo — offers unused aliases."""
    provided = _parse_repo_args(ctx.args)
    available = [a for a in _available_repo_aliases() if a not in provided]
    return [CompletionItem(name) for name in available if name.startswith(incomplete)]


def complete_workspace_name(ctx: typer.Context, incomplete: str) -> list[CompletionItem]:
    """Tab completion for workspace name."""
    try:
        root = _find_root()
        workspaces_dir = root / "workspaces"
        if workspaces_dir.exists():
            names = [
                d.name for d in workspaces_dir.iterdir()
                if d.is_dir() and (d / ".ow" / "config").exists()
            ]
            return [CompletionItem(name) for name in names if name.startswith(incomplete)]
    except FileNotFoundError:
        pass
    return []


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing files without backup"),
    force_with_backup: bool = typer.Option(False, "--force-with-backup", help="Backup existing files before overwrite"),
) -> None:
    """Initialize a new ow project."""
    cmd_init(path=None, force=force, with_backup=force_with_backup)


@app.command()
def create(
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Workspace name"),
    configuration: Optional[str] = typer.Option(None, "--configuration", "-c", help="Path to existing workspace config to duplicate"),
    template: Optional[list[str]] = typer.Option(None, "--template", "-t", help="Templates to apply (repeatable)", shell_complete=complete_gen_templates),
    repo: Optional[list[str]] = typer.Option(None, "--repo", "-r", help="Repo alias and branch spec (repeatable, e.g. -r community:master..x)", shell_complete=complete_gen_repos),
) -> None:
    """Create a new workspace."""
    config = _load_config()
    cmd_create(config, name=name, templates=template, repos=_parse_repo_value(repo), configuration=configuration)


@app.command()
def update(
    workspace: Optional[str] = typer.Argument(None, help="Workspace name (default: resolve from cwd)", shell_complete=complete_workspace_name),
) -> None:
    """Re-render templates and materialize worktrees."""
    config = _load_config()
    cmd_update(config, workspace=workspace)


@app.command()
def status(
    workspace: Optional[str] = typer.Argument(None, help="Workspace name (default: resolve from cwd)", shell_complete=complete_workspace_name),
) -> None:
    """Show workspace status."""
    config = _load_config()
    cmd_status(config, workspace=workspace)


@app.command()
def rebase(
    workspace: Optional[str] = typer.Argument(None, help="Workspace name (default: resolve from cwd)", shell_complete=complete_workspace_name),
) -> None:
    """Fetch and rebase workspace branches."""
    config = _load_config()
    cmd_rebase(config, workspace=workspace)


@app.command()
def prune() -> None:
    """Clean up stale worktree references and orphaned branches."""
    config = _load_config()
    cmd_prune(config)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
