"""CLI application builder for ow.

Every Typer signature lives here (a `commands/` module never imports `ow.cli`).
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, NoReturn, Sequence

import click
import typer
from typer.core import TyperCommand, TyperGroup

from . import __version__
from . import errors, term
from .core import git, tasks
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
DRY_RUN_HELP = "Print the commands without running them"
YES_HELP = "Skip the confirmation prompt"
JSON_HELP = "Emit machine-readable JSON on stdout"
TASK_HELP = "Task name grouping related workspaces"

# Registers the vendored click's shell-completion classes, which `shell-init` reads.
typer.completion.completion_init()


class _RunCommand(TyperCommand):
    """Capture everything after `--` as passthrough args before Click parses TARGET/options."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            ctx.meta["ow_run_passthrough"] = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            ctx.meta["ow_run_passthrough"] = ()
        return super().parse_args(ctx, args)


class OwGroup(TyperGroup):
    """Custom Typer group that handles no_args_is_help with exit 0 and OwError exceptions."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Override to handle bare 'ow' (no args) by printing help and exiting with 0.

        A completion request parses with empty args too, so fall through when parsing.
        """
        if not args and not ctx.resilient_parsing:
            term.out(ctx.get_help())
            ctx.exit(0)
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> None:
        """Override to turn an OwError into its exit code."""
        try:
            super().invoke(ctx)
        except errors.OwError as exc:
            raise typer.Exit(fail(exc)) from None


def _command_module(name: str) -> Any:
    """Import `ow.commands.<name>`."""
    return importlib.import_module(f"ow.commands.{name}")


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
            return []

    def _all_repo_names() -> list[str]:
        try:
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
            return list(dict.fromkeys(names))
        except Exception:
            return []

    def _repo_names(workspace: str | None = None) -> list[str]:
        try:
            session = _open(workspace, need_workspace=False)
            if session.workspace is not None:
                return [location.name for location in session.locations]
            return _all_repo_names()
        except Exception:
            return []

    def _task_names() -> list[str]:
        try:
            names: list[str] = []
            for entry in read_index():
                tag = tasks.read_task_tag(entry)
                if tag and tag not in names:
                    names.append(tag)
            return names
        except Exception:
            return []

    def _setting_keys() -> list[str]:
        try:
            return [setting.key for setting in plugins.settings()]
        except Exception:
            return []

    def complete_workspace(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _workspace_names() if name.startswith(incomplete)]

    def complete_repo(ctx: Any, incomplete: str) -> list[str]:
        workspace = (ctx.params or {}).get("workspace")
        return [name for name in _repo_names(workspace) if name.startswith(incomplete)]

    def complete_any_repo(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _all_repo_names() if name.startswith(incomplete)]

    def complete_task(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _task_names() if name.startswith(incomplete)]

    def complete_setting(ctx: Any, incomplete: str) -> list[str]:
        return [key for key in _setting_keys() if key.startswith(incomplete)]

    workspace_option = dict(metavar="WS", help=WORKSPACE_HELP, autocompletion=complete_workspace)
    only_option = dict(metavar="REPO", help=ONLY_HELP, autocompletion=complete_repo)
    task_option = dict(metavar="TASK", help=TASK_HELP, autocompletion=complete_task)

    # -- add -------------------------------------------------------------

    @app.command("add")
    def add_cmd(
        targets: list[str] = typer.Argument(
            ...,
            metavar="TARGET",
            help="PR target (e.g. enterprise#12345, URL, a+b) or fresh workspace name",
        ),
        name: str = typer.Option(
            None, "--name", metavar="NAME", help="Override the created workspace directory name"
        ),
        task: str = typer.Option(None, "-t", "--task", **task_option),
        base: str = typer.Option(
            None, "--base", metavar="REF", help="Base ref for added repos (a remote branch)"
        ),
        branch: str = typer.Option(
            None, "--branch", metavar="BRANCH", help="Create or check out this branch in every repo"
        ),
        repos: list[str] = typer.Option(
            None,
            "-r",
            "--repo",
            metavar="REPO",
            help="Repo to include (repeatable; defaults to odoo.default_repos on fresh workspaces)",
            autocompletion=complete_any_repo,
        ),
        json_output: bool = typer.Option(False, "--json", help=JSON_HELP),
    ) -> None:
        """Create workspaces from PR targets or a base ref."""
        git.check_version()
        _exit(
            _command_module("add").run(
                plugins,
                tuple(targets),
                name=name,
                task=task,
                base=base,
                branch=branch,
                repos=_only(repos),
                json_output=json_output,
            )
        )

    # -- run -------------------------------------------------------------

    @app.command(
        "run",
        cls=_RunCommand,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": False},
    )
    def run_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(
            None,
            metavar="TARGET",
            help="Optional PR target or workspace to create/reuse before running",
        ),
        tmp: bool = typer.Option(
            False,
            "--tmp",
            "--ephemeral",
            help="Create a temporary workspace and remove it on exit",
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Run a workspace (or create/reuse TARGET and run it)."""
        git.check_version()
        passthrough = tuple(ctx.meta.get("ow_run_passthrough", ()))
        extra_args = list(ctx.args) + list(passthrough)
        _exit(
            _command_module("run").run(
                plugins,
                target=target,
                workspace=workspace,
                tmp=tmp,
                extra_args=tuple(extra_args),
            )
        )

    # -- repo ------------------------------------------------------------

    repo_app = typer.Typer(
        cls=OwGroup,
        name="repo",
        help="Register, list and remove repos",
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
    )

    @repo_app.command("add")
    def repo_add(
        name: str = typer.Argument(
            ..., metavar="NAME", help="Repo name", autocompletion=complete_any_repo
        ),
        url: str = typer.Argument(
            None, metavar="URL", help="Clone URL (default: the URL ow knows for NAME)"
        ),
        base: str = typer.Option(
            None, "--base", metavar="REF", help="Default base for this repo, stored fully qualified"
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Register or clone a repo and add its worktree."""
        session = _open(workspace, need_workspace=False)
        _announce(session)
        git.check_version()
        _exit(_command_module("repo").add(session, name, url, base=base))

    @repo_app.command("ls")
    def repo_ls(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """List the known repos and their remotes."""
        session = _open(workspace, need_workspace=False)
        _announce(session)
        git.check_version()
        _exit(_command_module("repo").ls(session))

    @repo_app.command("rm")
    def repo_rm(
        name: str = typer.Argument(
            ...,
            metavar="NAME",
            help="Repo whose worktree this workspace loses",
            autocompletion=complete_repo,
        ),
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
        help=(
            "Bare `ow config` lists every setting with its effective value, its"
            " origin and its help; get, set or unset one with a subcommand."
        ),
        short_help="Read and write ow settings",
        invoke_without_command=True,
        add_completion=False,
        rich_markup_mode=None,
    )

    def _config_context(
        ctx: typer.Context, workspace: str | None, global_: bool, json_output: bool
    ) -> tuple[str | None, bool, bool]:
        """Merge a verb's own -w/--global/--json with the ones given before it."""
        parent = ctx.obj if isinstance(ctx.obj, dict) else {}
        return (
            workspace or parent.get("workspace"),
            global_ or bool(parent.get("global_")),
            json_output or bool(parent.get("json_output")),
        )

    @config_app.callback()
    def config_callback(
        ctx: typer.Context,
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global", help="Read or write the global config"),
        json_output: bool = typer.Option(False, "--json", help=JSON_HELP),
    ) -> None:
        """Show every setting with its effective value, its origin and its help."""
        ctx.obj = {"workspace": workspace, "global_": global_, "json_output": json_output}
        if ctx.invoked_subcommand is not None:
            return
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(
            _command_module("config").show_all(
                session, global_=global_, json_output=json_output
            )
        )

    @config_app.command("get")
    def config_get(
        ctx: typer.Context,
        key: str = typer.Argument(
            ..., metavar="KEY", help="Setting key", autocompletion=complete_setting
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global", help="Read the global layer"),
        json_output: bool = typer.Option(False, "--json", help=JSON_HELP),
    ) -> None:
        """Print one setting's effective value."""
        workspace, _global_, _json = _config_context(ctx, workspace, global_, json_output)
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(
            _command_module("config").get(
                session, key, global_=_global_, json_output=_json
            )
        )

    @config_app.command("set")
    def config_set(
        ctx: typer.Context,
        key: str = typer.Argument(
            ..., metavar="KEY", help="Setting key", autocompletion=complete_setting
        ),
        values: list[str] = typer.Argument(
            None,
            metavar="VALUE",
            help="One item per value; put `--` before a value that starts with a dash",
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(
            False, "--global", help="Write the global config instead of the workspace's"
        ),
    ) -> None:
        """Set a key, writing the workspace config unless --global is given."""
        workspace, global_, _json = _config_context(ctx, workspace, global_, False)
        if _json:
            raise errors.UsageError("--json is not supported on `ow config set`")
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(_command_module("config").set_(session, key, _only(values), global_=global_))

    @config_app.command("unset")
    def config_unset(
        ctx: typer.Context,
        key: str = typer.Argument(
            ..., metavar="KEY", help="Setting key", autocompletion=complete_setting
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(
            False, "--global", help="Write the global config instead of the workspace's"
        ),
    ) -> None:
        """Unset a key, writing the workspace config unless --global is given."""
        workspace, global_, _json = _config_context(ctx, workspace, global_, False)
        if _json:
            raise errors.UsageError("--json is not supported on `ow config unset`")
        session = _open(workspace, need_workspace=False)
        _announce(session)
        _exit(_command_module("config").unset(session, key, global_=global_))

    app.add_typer(config_app, name="config")

    # -- refresh, files --------------------------------------------------

    @app.command("refresh")
    def refresh_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Rewrite the generated files."""
        session = _open(workspace)
        _announce(session)
        _exit(_command_module("refresh").run(session))

    @app.command("files")
    def files_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        diff: bool = typer.Option(
            False,
            "--diff",
            help="Show the diff of every file that differs instead of the states",
        ),
        json_output: bool = typer.Option(False, "--json", help=JSON_HELP),
    ) -> None:
        """Show the generated files and their state."""
        session = _open(workspace)
        _announce(session)
        _exit(_command_module("files").run(session, diff=diff, json_output=json_output))

    # Add enabled plugin commands last, so the core surface is complete.
    plugins.add_commands(app)

    return app
