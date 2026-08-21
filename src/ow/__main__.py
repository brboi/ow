#!/usr/bin/env python3
import sys
import tomllib
from typing import Any, Optional

import typer

from ow.commands import (
    cmd_apply,
    cmd_init,
    cmd_ls,
    cmd_prune,
    cmd_rebase,
    cmd_status,
    cmd_templates,
)
from ow.utils import index
from ow.utils.config import Config, load_global_config, parse_branch_spec
from ow.utils.display import err_console
from ow.utils.legacy import check_legacy_layout
from ow.utils.paths import config_file
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
        # No "-v": that spelling is left free for a future --verbose. ow
        # shells out to git constantly, so verbosity is the flag most likely
        # to be wanted next, and the CLI surface freezes at 2.0.
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Odoo workspace manager."""
    pass


def _load_config() -> Config:
    """Load the user's global configuration, bootstrapping it on first use."""
    check_legacy_layout()
    try:
        return load_global_config()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        err_console.print(f"Error: could not load {config_file()}: {exc}", markup=False)
        raise typer.Exit(1)


def _available_repo_aliases() -> list[str]:
    """Return repo aliases from the global config in declaration order.

    Reads the config only if it already exists. Completion must never
    bootstrap it: load_global_config() would create a default config.toml,
    silently erasing the "no global config yet" condition that
    check_legacy_layout() depends on — the guard commands run through, but
    completion callbacks don't.
    """
    if not config_file().exists():
        return []
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
        templates = available_templates()
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
    """Tab completion for workspace name, from the discovery index.

    The same names `ow ls` shows. A workspace ow has never resolved is not in
    the index and so is not offered — completing a name it could not then
    resolve would be worse than offering nothing.
    """
    try:
        names = sorted({p.name for p in index.known_workspaces()})
    except Exception:
        # Completion must never crash the shell, whatever state the index is in.
        return []
    return [name for name in names if name.startswith(incomplete)]


# The four forms resolve_workspace() accepts, in the order it tries them.
# Naming only the first two is how someone fresh out of the migration — with
# workspaces on disk that the index has never seen — reads "Workspace name",
# tries the name, and gets told to pass a path the help never mentioned.
WORKSPACE_HELP = (
    "Workspace to act on: a name ow ls knows, or a path such as ./myws "
    "(default: $OW_WORKSPACE, else the workspace holding the current directory)"
)


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
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated repo aliases to materialize (default: all); templates still render from the whole config, so addons_path may reference repos not yet materialized"),
) -> None:
    """Re-render templates and materialize worktrees."""
    config = _load_config()
    cmd_apply(config, workspace=workspace, only=only)


@app.command()
def status(
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
) -> None:
    """Show workspace status."""
    config = _load_config()
    cmd_status(config, workspace=workspace)


@app.command()
def rebase(
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
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
def ls() -> None:
    """List every known workspace, its path, and its repos."""
    cmd_ls()


@app.command()
def prune() -> None:
    """Clean up stale worktree references and orphaned branches."""
    config = _load_config()
    cmd_prune(config)


@app.command()
def templates(
    take: Optional[str] = typer.Option(None, "--take", help="Copy a packaged template file (BUNDLE/PATH) into your config, keeping a pristine baseline"),
    diff: bool = typer.Option(False, "--diff", help="Show what ow changed in the files you took, baseline against packaged (ignored if --take is also given)"),
) -> None:
    """List template files and their state, or take one."""
    cmd_templates(take=take, show_diff=diff)


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
