"""CLI application builder for ow.

Every Typer signature lives here (a `commands/` module never imports `ow.cli`).
Each command body opens its session, prints the session's warnings, checks the
git version and validates `--only` *before* importing its `ow.commands` module:
the import stays lazy so a later wave's module can be absent, but a wired
command must still report its own usage errors (exit 2) rather than "not
implemented yet".
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Sequence

import click
import typer
from typer.core import TyperGroup

from . import __version__
from . import errors, term
from .core import git
from .core.index import read as read_index
from .core.paths import repos_dir
from .core.session import Session, open_session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


WORKSPACE_HELP = (
    "Workspace to act on, by name or path (default: the workspace holding the"
    " current directory)"
)
ONLY_HELP = "Limit the command to this repo (repeatable; default: every repo)"
DRY_RUN_HELP = "Print the git commands without running them"
YES_HELP = "Skip the confirmation prompt"
NO_FETCH_HELP = "Do not fetch first; use the cached refs"

# `shell-init` owns completion (add_completion=False keeps --install-completion off),
# but the classes must still be registered for a live `ow` to answer _OW_COMPLETE.
typer.completion.completion_init()


class OwGroup(TyperGroup):
    """Custom Typer group that handles no_args_is_help with exit 0 and OwError exceptions."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Override to handle bare 'ow' (no args) by printing help and exiting with 0.

        A completion request parses with empty args too, so the env var typer's
        completion machinery sets (§14: `_<prog>_COMPLETE`) must fall through.
        """
        if not args and not os.environ.get("_OW_COMPLETE"):
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


def _command_module(name: str) -> Any:
    """Import `ow.commands.<name>`; a module of a later wave is refused, never a traceback."""
    try:
        return importlib.import_module(f"ow.commands.{name}")
    except ImportError as exc:
        raise errors.Refused(f"`ow {name}` is not implemented yet ({exc})") from None


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

    # -- session plumbing ------------------------------------------------

    def _open(workspace: str | None, *, need_workspace: bool = True) -> Session:
        return open_session(workspace, need_workspace=need_workspace, plugins=plugins)

    def _announce(session: Session) -> None:
        for warning in session.warnings:
            term.warn(warning)

    def _only(only: list[str] | None) -> tuple[str, ...]:
        return tuple(only or ())

    def _validate_only(session: Session, only: Sequence[str] | None) -> None:
        names = [location.name for location in session.locations]
        for repo in only or ():
            if repo not in names:
                valid = ", ".join(names) if names else "(none)"
                raise errors.UsageError(
                    f"unknown repo {repo!r} in --only; this workspace has: {valid}"
                )

    def _checked(workspace: str | None, only: list[str] | None = None) -> Session:
        """The session a git command starts from: opened, announced, version-checked, --only checked."""
        session = _open(workspace)
        _announce(session)
        git.check_version()
        _validate_only(session, only)
        return session

    def _exit(result: int | None) -> NoReturn:
        """Commands return their exit code; the lifecycle ones return nothing (exit 0)."""
        raise typer.Exit(0 if result is None else result)

    # -- completers ------------------------------------------------------

    def _workspace_names() -> list[str]:
        try:
            names: list[str] = []
            for entry in read_index():
                if entry.name not in names:
                    names.append(entry.name)
            return names
        except Exception:
            # Completion must never crash the shell, whatever state the index is in.
            return []

    def _repo_names() -> list[str]:
        try:
            session = _open(None, need_workspace=False)
            if session.workspace is not None:
                return [location.name for location in session.locations]
            names: list[str] = []
            directory = repos_dir()
            if directory.is_dir():
                names += [
                    entry.stem
                    for entry in directory.iterdir()
                    if entry.name.endswith(".git") and not entry.name.startswith(".")
                ]
            for _plugin, known in plugins.call("ow_known_repos"):
                names += [repo.name for repo in known]
            # repos_dir() and ow_known_repos overlap; a shell hates duplicates.
            return list(dict.fromkeys(names))
        except Exception:
            # Best-effort: completion must never crash the shell.
            return []

    def _setting_keys() -> list[str]:
        try:
            return [setting.key for setting in plugins.settings()]
        except Exception:
            return []

    def complete_workspace(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _workspace_names() if name.startswith(incomplete)]

    def complete_repo(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _repo_names() if name.startswith(incomplete)]

    def complete_setting(ctx: Any, incomplete: str) -> list[str]:
        return [key for key in _setting_keys() if key.startswith(incomplete)]

    workspace_option = dict(help=WORKSPACE_HELP, autocompletion=complete_workspace)
    only_option = dict(help=ONLY_HELP, autocompletion=complete_repo)

    # -- init ------------------------------------------------------------

    @app.command("init")
    def init_cmd(
        directory: Path = typer.Argument(Path("."), metavar="DIR",
            help="Where the workspace lives (default: the current directory)"),
        repos: list[str] = typer.Argument(None, metavar="REPO",
            help="Repos to add to the workspace"),
        base: str = typer.Option(None, "--base", metavar="REF",
            help="Default base for the repos ow adds (a remote branch)"),
        branch: str = typer.Option(None, "--branch", metavar="BRANCH",
            help="Create and switch to this branch in every repo"),
    ) -> None:
        """Create, adopt or repair the workspace at DIR."""
        git.check_version()
        module = _command_module("init")
        _exit(module.run(plugins, directory, _only(repos), base=base, branch=branch))

    # -- repo ------------------------------------------------------------

    repo_app = typer.Typer(
        name="repo",
        help="Register, list and remove repos",
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
    )

    @repo_app.command("add")
    def repo_add(
        name: str = typer.Argument(..., metavar="NAME", help="Repo name"),
        url: str = typer.Argument(None, metavar="URL",
            help="Clone URL (default: the URL ow knows for NAME)"),
        base: str = typer.Option(None, "--base", metavar="REF",
            help="Default base for this repo, stored fully qualified"),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Register or clone a repo, and add its worktree when inside a workspace."""
        session = _open(workspace, need_workspace=False)
        _announce(session)
        git.check_version()
        _exit(_command_module("repo").add(session, name, url, base=base))

    @repo_app.command("ls")
    def repo_ls(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """List the known repos and their remotes, marking the ones this workspace uses."""
        session = _open(workspace, need_workspace=False)
        _announce(session)
        git.check_version()
        _exit(_command_module("repo").ls(session))

    @repo_app.command("rm")
    def repo_rm(
        name: str = typer.Argument(..., metavar="NAME",
            help="Repo whose worktree this workspace loses"),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Remove a repo's worktree from this workspace."""
        session = _checked(workspace)
        _exit(_command_module("repo").rm(session, name, dry_run=dry_run, yes=yes))

    app.add_typer(repo_app, name="repo")

    # -- config ----------------------------------------------------------

    config_app = typer.Typer(
        name="config",
        help="Read and write ow settings",
        invoke_without_command=True,
        add_completion=False,
        rich_markup_mode=None,
    )

    def _config_context(
        ctx: typer.Context, workspace: str | None, global_: bool
    ) -> tuple[str | None, bool]:
        """Merge a verb's own -w/--global with the ones given before it."""
        parent = ctx.obj if isinstance(ctx.obj, dict) else {}
        return (
            workspace or parent.get("workspace"),
            global_ or bool(parent.get("global_")),
        )

    @config_app.callback()
    def config_callback(
        ctx: typer.Context,
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global",
            help="Read or write the global config"),
    ) -> None:
        """Show every setting with its effective value, its origin and its help."""
        ctx.obj = {"workspace": workspace, "global_": global_}
        if ctx.invoked_subcommand is not None:
            return
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(_command_module("config").show_all(session, global_=global_))

    @config_app.command("get")
    def config_get(
        ctx: typer.Context,
        key: str = typer.Argument(..., metavar="KEY", help="Setting key",
            autocompletion=complete_setting),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global", help="Read the global layer"),
    ) -> None:
        """Print one setting's effective value."""
        workspace, _global_ = _config_context(ctx, workspace, global_)
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(_command_module("config").get(session, key))

    @config_app.command("set")
    def config_set(
        ctx: typer.Context,
        key: str = typer.Argument(..., metavar="KEY", help="Setting key",
            autocompletion=complete_setting),
        values: list[str] = typer.Argument(None, metavar="VALUE",
            help="One item per value; put `--` before a value that starts with a dash"),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global",
            help="Write the global config instead of the workspace's"),
    ) -> None:
        """Set a key, writing the workspace config unless --global is given."""
        workspace, global_ = _config_context(ctx, workspace, global_)
        # set_/unset raise their own "not inside an ow workspace" usage error (§9).
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(_command_module("config").set_(
            session, key, _only(values), global_=global_))

    @config_app.command("unset")
    def config_unset(
        ctx: typer.Context,
        key: str = typer.Argument(..., metavar="KEY", help="Setting key",
            autocompletion=complete_setting),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global",
            help="Write the global config instead of the workspace's"),
    ) -> None:
        """Unset a key, writing the workspace config unless --global is given."""
        workspace, global_ = _config_context(ctx, workspace, global_)
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(_command_module("config").unset(session, key, global_=global_))

    app.add_typer(config_app, name="config")

    # -- status, fetch, switch, pull, rebase, reset -----------------------

    @app.command("status")
    def status_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        # §4 gives status no --only; it is accepted so the shared validation
        # applies (`ow status --only <bogus>` must name the valid repos), then
        # ignored: the table always shows the whole workspace.
        only: list[str] = typer.Option(None, "--only", **only_option),
    ) -> None:
        """Show each repo's branch, base, upstream, ahead/behind and dirty count."""
        session = _checked(workspace, only)
        _exit(_command_module("status").run(session))

    @app.command("fetch")
    def fetch_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        only: list[str] = typer.Option(None, "--only", **only_option),
    ) -> None:
        """Run the fetches it shows, then print the status table."""
        session = _checked(workspace, only)
        _exit(_command_module("fetch").run(session, only=_only(only)))

    @app.command("switch")
    def switch_cmd(
        target: str = typer.Argument(None, metavar="BRANCH",
            help="Branch every repo switches to"),
        create: str = typer.Option(None, "-c", "--create", metavar="BRANCH",
            help="Create this branch instead of switching to an existing one"),
        base: str = typer.Option(None, "--base", metavar="REF",
            help="Base for -c, stored fully qualified"),
        detach: bool = typer.Option(False, "--detach",
            help="Detach at each repo's default base"),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        only: list[str] = typer.Option(None, "--only", **only_option),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
    ) -> None:
        """Switch the workspace's branch."""
        chosen = [target is not None, create is not None, detach]
        if sum(chosen) != 1:
            raise errors.UsageError(
                "pass exactly one of <branch>, -c <branch> or --detach"
            )
        session = _checked(workspace, only)
        _exit(_command_module("switch").run(
            session, target, create=create, base=base, detach=detach,
            only=_only(only), dry_run=dry_run))

    @app.command("pull")
    def pull_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        only: list[str] = typer.Option(None, "--only", **only_option),
        no_fetch: bool = typer.Option(False, "--no-fetch", help=NO_FETCH_HELP),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
    ) -> None:
        """Fetch, then fast-forward each repo to the ref it follows."""
        session = _checked(workspace, only)
        _exit(_command_module("pull").run(
            session, only=_only(only), no_fetch=no_fetch, dry_run=dry_run))

    @app.command("rebase")
    def rebase_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        only: list[str] = typer.Option(None, "--only", **only_option),
        base: str = typer.Option(None, "--base", metavar="REF",
            help="Rebase onto this ref and record it as the new ow-base"),
        autostash: bool = typer.Option(False, "--autostash",
            help="Stash and restore uncommitted changes around each rebase"),
        no_fetch: bool = typer.Option(False, "--no-fetch", help=NO_FETCH_HELP),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Fetch, then run the guarded rebase recipe."""
        session = _checked(workspace, only)
        _exit(_command_module("rebase").run(
            session, only=_only(only), base=base, autostash=autostash,
            no_fetch=no_fetch, dry_run=dry_run, yes=yes))

    @app.command("reset")
    def reset_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        only: list[str] = typer.Option(None, "--only", **only_option),
        hard: bool = typer.Option(False, "--hard",
            help="Discard the working tree too, not just the commits"),
        no_fetch: bool = typer.Option(False, "--no-fetch", help=NO_FETCH_HELP),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Fetch, then put each repo back on the ref it follows."""
        session = _checked(workspace, only)
        _exit(_command_module("reset").run(
            session, only=_only(only), hard=hard, no_fetch=no_fetch,
            dry_run=dry_run, yes=yes))

    # -- refresh, files --------------------------------------------------

    @app.command("refresh")
    def refresh_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Recompute and write the generated files."""
        session = _open(workspace)
        _announce(session)
        _exit(_command_module("refresh").run(session))

    @app.command("files")
    def files_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        diff: bool = typer.Option(False, "--diff",
            help="Show the diff of every file that differs instead of the states"),
    ) -> None:
        """List the generated files and their states."""
        session = _open(workspace)
        _announce(session)
        _exit(_command_module("files").run(session, diff=diff))

    # -- fleet: ls, cd, shell-init, rm, prune ----------------------------

    @app.command("ls")
    def ls_cmd() -> None:
        """List the known workspaces, each repo's branch or detached base."""
        git.check_version()
        _exit(_command_module("ls").run(plugins))

    @app.command("cd")
    def cd_cmd(
        ws: str = typer.Argument(None, metavar="WS",
            help="Workspace to print, by name or path (default: the one holding the"
                 " current directory)",
            autocompletion=complete_workspace),
    ) -> None:
        """Print a workspace's path."""
        session = _open(ws)
        _announce(session)
        _exit(_command_module("cd").run(session))

    @app.command("shell-init")
    def shell_init_cmd(
        shell: str = typer.Argument(..., metavar="SHELL", help="bash, zsh or fish"),
    ) -> None:
        """Print the `ow cd` wrapper and the completion script for SHELL."""
        if shell not in ("bash", "zsh", "fish"):
            raise typer.BadParameter(
                f"unsupported shell {shell!r}; expected bash, zsh or fish"
            )
        _exit(_command_module("shell_init").run(shell, app=app))

    @app.command("rm")
    def rm_cmd(
        ws: str = typer.Argument(..., metavar="WS", help="Workspace to remove",
            autocompletion=complete_workspace),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Remove a workspace: its worktrees, pushed branches, directory and index entry."""
        session = _checked(ws)
        _exit(_command_module("rm").run(session, dry_run=dry_run, yes=yes))

    @app.command("prune")
    def prune_cmd(
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Clean machine-wide: stale worktree references, pushed orphan branches, dead index entries."""
        git.check_version()
        _exit(_command_module("prune").run(plugins, dry_run=dry_run, yes=yes))

    # Add plugin command groups last, so the core surface is complete.
    plugins.add_commands(app)

    return app
