#!/usr/bin/env python3
import sys
import tomllib
from typing import Any, Optional

import typer

from ow.commands import (
    cmd_apply,
    cmd_archive,
    cmd_cd,
    cmd_init,
    cmd_ls,
    cmd_mv,
    cmd_open,
    cmd_prune,
    cmd_rebase,
    cmd_rm,
    cmd_shell_init,
    cmd_status,
    cmd_templates,
    cmd_unarchive,
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
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
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
        names = sorted({p.name for p in index.list_workspaces()})
    except Exception:
        # Completion must never crash the shell, whatever state the index is in.
        return []
    return [name for name in names if name.startswith(incomplete)]


def complete_archived_name(ctx: typer.Context, incomplete: str) -> list[str]:
    """Tab completion for an archived workspace name.

    Reads the archive directory, not the index: an archived workspace is by
    definition not in the index.
    """
    from ow.commands.archive import archived_workspaces

    return [
        d.name for d in archived_workspaces() if d.name.startswith(incomplete)
    ]


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
    check: bool = typer.Option(False, "--check", help="Report drift and outdated templates without modifying anything; exit non-zero if either is found"),
) -> None:
    """Re-render templates and materialize worktrees."""
    config = _load_config()
    cmd_apply(config, workspace=workspace, check=check)


@app.command()
def status(
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
    fetch: bool = typer.Option(False, "--fetch", "-f", help="Fetch refs before showing status."),
) -> None:
    """Show workspace status."""
    config = _load_config()
    cmd_status(config, workspace=workspace, fetch=fetch)


@app.command()
def rebase(
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated repo aliases to rebase (default: all)"),
    autostash: bool = typer.Option(False, "--autostash", help="Stash and restore uncommitted changes around each rebase"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the git commands without running them"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Rebase against cached refs without fetching"),
) -> None:
    """Fetch and rebase workspace branches."""
    config = _load_config()
    cmd_rebase(
        config, workspace=workspace, only=only,
        autostash=autostash, dry_run=dry_run, yes=yes, no_fetch=no_fetch,
    )


@app.command()
def ls(
    archived: bool = typer.Option(False, "--archived", help="List archived workspaces instead of active ones"),
) -> None:
    """List every known workspace, its path, and its repos."""
    cmd_ls(archived=archived)


@app.command()
def rm(
    name: str = typer.Argument(..., help="Workspace name (as shown by ow ls)", autocompletion=complete_workspace_name),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Remove a workspace: worktrees, local branches, directory, and index entry."""
    # Same as prune and ls: no global config needed, no bootstrap.
    check_legacy_layout()
    cmd_rm(name=name, yes=yes)


@app.command()
def mv(
    source: str = typer.Argument(..., help="Workspace name or path", autocompletion=complete_workspace_name),
    dest: str = typer.Argument(..., help="New path, or an existing directory to move into"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Move a workspace to a new path, repairing its worktrees."""
    config = _load_config()
    cmd_mv(config, source=source, dest=dest, yes=yes)


@app.command()
def archive(
    name: str = typer.Argument(..., help="Workspace name (as shown by ow ls)", autocompletion=complete_workspace_name),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Move a workspace into the archive, keeping its worktrees and branches."""
    # Same as rm, prune and ls: no global config needed, no bootstrap.
    check_legacy_layout()
    cmd_archive(name=name, yes=yes)


@app.command()
def unarchive(
    name: str = typer.Argument(..., help="Archived workspace name (as shown by ow ls --archived)", autocompletion=complete_archived_name),
    dest: Optional[str] = typer.Argument(None, help="Where to restore it (default: ./NAME)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Restore an archived workspace."""
    config = _load_config()
    cmd_unarchive(config, name=name, dest=dest, yes=yes)


@app.command()
def prune(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the cleanup plan and dead index entries without making any changes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    also_backups: bool = typer.Option(False, "--also-backups", help="Also delete every saved `ow rm` config backup"),
) -> None:
    """Clean up stale worktree references, orphaned branches, and dead index entries."""
    # Not _load_config(): prune reads no global config, and bootstrapping one
    # here would write a default config.toml for a command that never opens
    # it. The legacy gate is the only part of that path prune needs.
    check_legacy_layout()
    cmd_prune(dry_run=dry_run, yes=yes, also_backups=also_backups)

@app.command()
def templates(
    take: Optional[str] = typer.Option(None, "--take", help="Copy a packaged template file (BUNDLE/PATH) into your config, keeping a pristine baseline"),
    diff: bool = typer.Option(False, "--diff", help="Show what ow changed in the files you took, baseline against packaged (ignored if --take is also given)"),
) -> None:
    """List template files and their state, or take one."""
    cmd_templates(take=take, show_diff=diff)


@app.command()
def cd(
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
) -> None:
    """Print a workspace path — with `ow shell-init`, changes directory."""
    # Warn, never stop: cd is read-only and is where a lost user is sent.
    check_legacy_layout(fatal=False)
    cmd_cd(workspace)


@app.command(name="shell-init")
def shell_init(
    shell: str = typer.Argument(..., help="bash, zsh, or fish"),
) -> None:
    """Print the shell snippet that makes `ow cd` change directory."""
    cmd_shell_init(shell)


@app.command(name="open")
def open_ws(
    workspace: Optional[str] = typer.Argument(None, help=WORKSPACE_HELP, autocompletion=complete_workspace_name),
) -> None:
    """Open a workspace in the configured editor."""
    config = _load_config()
    cmd_open(config, workspace=workspace)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        # 130 is the conventional shell status for SIGINT. The git children
        # are already gone, pool or no pool: each one runs in its own session
        # so the terminal's SIGINT never reached it, and _run kills the child
        # it was waiting on when the interrupt lands in communicate().
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)



if __name__ == "__main__":
    main()
