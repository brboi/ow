"""CLI application builder for ow.

Every Typer signature lives here (a `commands/` module never imports `ow.cli`).
"""
from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

import typer
from typer._click.exceptions import ClickException
from typer._click.exceptions import UsageError as ClickUsageError
from typer.core import TyperCommand, TyperGroup

from . import __version__, errors, term
from .core import git, stacks
from .core.index import read as read_index
from .core.paths import repos_dir
from .core.session import Session, open_session, workspace_plugins
from .core.workspace import resolve_target_root

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


WORKSPACE_HELP = (
    "Workspace or stack (WS[/STACK]) to act on, by name or path (default: current directory's workspace)"
)
ONLY_HELP = "Limit the command to this repo (repeatable; default: every repo)"
DRY_RUN_HELP = "Print the commands without running them"
YES_HELP = "Skip the confirmation prompt"

# Registers the vendored click's shell-completion classes, which `shell-init` reads.
typer.completion.completion_init()


# click's marker for a required argument in the usage line (`{WS}`).
_REQUIRED_MARK = re.compile(r"\{(\w+)\}")
# typer's type tag after an argument in the help listing (`WS <list[str]>`).
_TYPE_TAG = re.compile(r" <[^>]*>$")


class OwCommand(TyperCommand):
    """A command whose help names each argument by its metavar alone."""

    def collect_usage_pieces(self, ctx: typer.Context) -> list[str]:
        return [_REQUIRED_MARK.sub(r"\1", piece) for piece in super().collect_usage_pieces(ctx)]

    def format_options(self, ctx: typer.Context, formatter: Any) -> None:
        arguments: list[tuple[str, str]] = []
        options: list[tuple[str, str]] = []
        for param in self.get_params(ctx):
            record = param.get_help_record(ctx)
            if record is None:
                continue
            if param.param_type_name == "argument":
                arguments.append((_TYPE_TAG.sub("", record[0]), record[1]))
            else:
                options.append(record)
        for title, records in (("Arguments", arguments), ("Options", options)):
            if records:
                with formatter.section(title):
                    formatter.write_dl(records)


class _PassthroughCommand(OwCommand):
    """Capture everything after `--` as passthrough args before Click parses WS/options."""

    passthrough: ClassVar[str]

    def collect_usage_pieces(self, ctx: typer.Context) -> list[str]:
        return [*super().collect_usage_pieces(ctx), f"[-- {self.passthrough}]"]

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            ctx.meta["ow_run_passthrough"] = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            ctx.meta["ow_run_passthrough"] = ()
        return super().parse_args(ctx, args)


class _RunCommand(_PassthroughCommand):
    passthrough = "ARGS..."


class _ExecCommand(_PassthroughCommand):
    passthrough = "COMMAND..."


class OwGroup(TyperGroup):
    """Custom Typer group that handles no_args_is_help with exit 0, OwError and usage errors."""

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        """Override to handle bare 'ow' (no args) by printing help and exiting with 0.

        A completion request parses with empty args too, so fall through when parsing.
        """
        if not args and not ctx.resilient_parsing:
            term.out(ctx.get_help())
            ctx.exit(0)
        return super().parse_args(ctx, args)

    def make_context(self, info_name: str | None, args: list[str], **extra: Any) -> typer.Context:
        """Override to report a usage error of this group's own arguments as ow does."""
        try:
            return super().make_context(info_name, args, **extra)
        except ClickException as exc:
            raise typer.Exit(fail_usage(exc)) from None

    def invoke(self, ctx: typer.Context) -> None:
        """Override to turn an OwError, or a usage error of a subcommand, into its exit code."""
        try:
            super().invoke(ctx)
        except errors.OwError as exc:
            raise typer.Exit(fail(exc)) from None
        except ClickException as exc:
            raise typer.Exit(fail_usage(exc)) from None


def _command_module(name: str) -> Any:
    """Import `ow.commands.<name>`."""
    return importlib.import_module(f"ow.commands.{name}")


def fail(exc: errors.OwError) -> int:
    """Print an expected failure and return its exit code: the one OwError handler."""
    term.error(str(exc))
    return exc.exit_code


def fail_usage(exc: ClickException) -> int:
    """Print a click/typer usage failure like an OwError, after the usage lines that help, and return its exit code."""
    if isinstance(exc, ClickUsageError) and exc.ctx is not None:
        ctx = exc.ctx
        term.err(ctx.get_usage())
        if ctx.command.get_help_option(ctx) is not None:
            term.err(f"Try '{ctx.command_path} {ctx.help_option_names[0]}' for help.")
    term.error(exc.format_message())
    return exc.exit_code


def build_app(plugins: Plugins) -> typer.Typer:
    """Build the Typer application with plugins."""

    def version_callback(value: bool) -> None:
        if value:
            term.out(f"ow {__version__}")
            raise typer.Exit()

    app = typer.Typer(
        cls=OwGroup,
        help="Ready-to-code Odoo workspaces on git worktrees.",
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
    )
    command = partial(app.command, cls=OwCommand)

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

    def _open(
        workspace: str | None,
        *,
        need_workspace: bool = True,
        ignore_global: Sequence[str] = (),
        ignore_workspace: Sequence[str] = (),
    ) -> Session:
        return open_session(
            workspace,
            need_workspace=need_workspace,
            plugins=plugins,
            ignore_global=ignore_global,
            ignore_workspace=ignore_workspace,
        )

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

    def _opened(workspace: str | None) -> Session:
        """The session of `workspace`, opened and with its warnings announced."""
        session = _open(workspace)
        _announce(session)
        return session

    def _checked(workspace: str | None, only: list[str] | None = None) -> Session:
        """The session a git command starts from: opened, announced, version-checked, --only checked."""
        session = _opened(workspace)
        git.check_version()
        _validate_only(session, only)
        return session

    def _single_root(workspace: str | None, on_multi: Callable[[Path], int]) -> str:
        """The root of the workspace or stack `workspace` names; a multi-stack root goes to `on_multi` and exits."""
        root, kind = resolve_target_root(workspace)
        if kind == "multi":
            _exit(on_multi(root))
        return str(root)

    def _targets(names: str | Sequence[str] | None, workspace: str | None) -> tuple[str, ...]:
        """The WS argument(s) and `-w` as one list of targets (empty: the current workspace)."""
        named = (names,) if isinstance(names, str) else tuple(names or ())
        if named and workspace is not None:
            raise errors.UsageError("pass either WS or -w/--workspace, not both")
        return named or (() if workspace is None else (workspace,))

    def _exit(result: int | None) -> NoReturn:
        """Commands return their exit code; the lifecycle ones return nothing (exit 0)."""
        raise typer.Exit(0 if result is None else result)

    # -- completers ------------------------------------------------------

    def _workspace_names() -> list[str]:
        try:
            names: list[str] = []
            cwd = Path.cwd()
            local_stacks = stacks.detect_multi_workspace(cwd)
            if local_stacks:
                for sp in local_stacks:
                    if sp.name not in names:
                        names.append(sp.name)
            for entry in read_index():
                if not entry.exists():
                    continue
                if entry.name not in names:
                    names.append(entry.name)
                child_stacks = stacks.detect_multi_workspace(entry)
                if child_stacks:
                    for sp in child_stacks:
                        qualified = f"{entry.name}/{sp.name}"
                        if qualified not in names:
                            names.append(qualified)
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

    def _setting_keys() -> list[str]:
        try:
            return [setting.key for setting in plugins.settings() if not setting.renamed_to]
        except Exception:
            return []

    def complete_workspace(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _workspace_names() if name.startswith(incomplete)]

    def complete_repo(ctx: Any, incomplete: str) -> list[str]:
        workspace = (ctx.params or {}).get("workspace")
        return [name for name in _repo_names(workspace) if name.startswith(incomplete)]

    def complete_any_repo(ctx: Any, incomplete: str) -> list[str]:
        return [name for name in _all_repo_names() if name.startswith(incomplete)]

    def complete_setting(ctx: Any, incomplete: str) -> list[str]:
        return [key for key in _setting_keys() if key.startswith(incomplete)]

    workspace_option = dict(metavar="WS", help=WORKSPACE_HELP, autocompletion=complete_workspace)
    only_option = dict(metavar="REPO", help=ONLY_HELP, autocompletion=complete_repo)

    # -- init ------------------------------------------------------------

    @command("init")
    def init_cmd(
        specs: list[str] = typer.Argument(
            None,
            metavar="SPEC",
            help="Spec(s): @18.0, enterprise#12345, enterprise@18.0..18.0-fix, a=community#1,enterprise#2, voip",
        ),
        as_name: str = typer.Option(
            None,
            "--as",
            metavar="STACK",
            help="Stack name for the existing workspace when promoting it to multi-stack",
        ),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Initialize, compose, adopt, or repair the current directory."""
        git.check_version()
        _exit(
            _command_module("init").run(
                plugins,
                tuple(specs or ()),
                as_name=as_name,
                dry_run=dry_run,
                yes=yes,
            )
        )

    # -- add -------------------------------------------------------------

    @command("add")
    def add_cmd(
        specs: list[str] = typer.Argument(
            ...,
            metavar="SPEC",
            help="Repo, PR, or named stack spec to add inside the current workspace",
        ),
        as_name: str = typer.Option(
            None,
            "--as",
            metavar="STACK",
            help="Stack name for the existing workspace when promoting it to multi-stack",
        ),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Add repos, attach PRs, or add stacks to the current workspace."""
        git.check_version()
        _exit(
            _command_module("add").run(
                plugins,
                tuple(specs),
                as_name=as_name,
                dry_run=dry_run,
                yes=yes,
            )
        )

    # -- run -------------------------------------------------------------

    @command(
        "run",
        cls=_RunCommand,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": False},
    )
    def run_cmd(
        ctx: typer.Context,
        items: list[str] = typer.Argument(
            None,
            metavar="WS",
            help='Workspace or stack name(s) (WS[/STACK]), or SPEC(s) with --tmp, optionally with :"ARGS"',
        ),
        tmp: bool = typer.Option(
            False,
            "--tmp",
            "--ephemeral",
            help="Create temporary stack(s) from SPEC arguments and remove them on exit",
        ),
        fail_fast: bool = typer.Option(
            False,
            "--fail-fast",
            help="Stop all running stacks as soon as one exits non-zero",
        ),
        reset: bool = typer.Option(
            False,
            "--reset",
            help="Drop each launched stack's database and local filestore before starting (asks first)",
        ),
        yes: bool = typer.Option(False, "-y", "--yes", help="Do not ask before --reset drops anything"),
        detach: bool = typer.Option(
            False,
            "-d",
            "--detach",
            help="Run workspace/stack(s) in the background (stop with `ow stop`, view logs with `ow logs`)",
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Run a workspace (foreground or detached with -d).

        Everything after `--` is passed to the runner.
        """
        git.check_version()
        passthrough = tuple(ctx.meta.get("ow_run_passthrough", ()))
        extra_args = list(ctx.args) + list(passthrough)
        _exit(
            _command_module("run").run(
                plugins,
                items=tuple(items or ()),
                workspace=workspace,
                tmp=tmp,
                fail_fast=fail_fast,
                reset=reset,
                yes=yes,
                detach=detach,
                extra_args=tuple(extra_args),
            )
        )

    # -- stop ------------------------------------------------------------

    @command("stop")
    def stop_cmd(
        ws_names: list[str] = typer.Argument(
            None,
            metavar="WS",
            help="Workspace or stack name(s) (WS[/STACK]) to stop (default: current workspace)",
        ),
        services: bool = typer.Option(
            False,
            "--services",
            help="Stop shared global service containers (postgres, pgweb, mailsink)",
        ),
        all_workspaces: bool = typer.Option(
            False,
            "--all",
            help="Stop every running indexed workspace and all shared global services",
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Stop detached workspace/stack(s) or global services."""
        _exit(
            _command_module("stop").run(
                plugins,
                ws_names=_targets(ws_names, workspace),
                services=services,
                all_workspaces=all_workspaces,
            )
        )

    # -- logs ------------------------------------------------------------

    @command("logs")
    def logs_cmd(
        ws_names: list[str] = typer.Argument(
            None,
            metavar="WS",
            help="Workspace or stack name(s) (WS[/STACK]) whose logs to show (default: current workspace)",
        ),
        follow: bool = typer.Option(
            False,
            "-f",
            "--follow",
            help="Follow log output until the process stops (or Ctrl+C)",
        ),
        lines: int = typer.Option(
            50,
            "-n",
            "--lines",
            metavar="LINES",
            help="Number of trailing lines to show per log file",
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """View or follow logs of a detached workspace or stack."""
        _exit(
            _command_module("logs").run(
                plugins,
                ws_names=_targets(ws_names, workspace),
                follow=follow,
                lines=lines,
            )
        )

    # -- exec ------------------------------------------------------------

    @command(
        "exec",
        cls=_ExecCommand,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": False},
    )
    def exec_cmd(
        ctx: typer.Context,
        ws_name: str = typer.Argument(
            None,
            metavar="WS",
            help="Workspace or stack (WS[/STACK]) to enter (default: current directory)",
            autocompletion=complete_workspace,
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Run a command or shell in a stack's virtualenv.

        Everything after `--` is the command; without one, starts a shell.
        """
        git.check_version()
        if ctx.args:
            hint_tokens = ([ws_name] if ws_name is not None else []) + list(ctx.args)
            hint = " ".join(hint_tokens)
            raise errors.UsageError(
                f"unexpected arguments before `--`; pass commands after `--` (e.g. `ow exec -- {hint}`)"
            )
        command = tuple(ctx.meta.get("ow_run_passthrough", ()))
        target = _targets(ws_name, workspace)
        _exit(
            _command_module("exec").run(
                plugins,
                ws_name=target[0] if target else None,
                command=command,
            )
        )

    # -- sandbox ---------------------------------------------------------

    @command(
        "sandbox",
        cls=_ExecCommand,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": False},
    )
    def sandbox_cmd(
        ctx: typer.Context,
        ws_name: str = typer.Argument(
            None,
            metavar="WS",
            help="Workspace or stack (WS[/STACK]) to sandbox (default: current directory)",
            autocompletion=complete_workspace,
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        profile: str = typer.Option(
            None,
            "-p",
            "--profile",
            metavar="PROFILE",
            help="Harness profile: auto, claude, opencode, codex, pi, omp, or none",
        ),
        ro: list[str] = typer.Option(
            None, "--ro", metavar="PATH", help="Extra host path to mount read-only (repeatable)"
        ),
        rw: list[str] = typer.Option(
            None, "--rw", metavar="PATH", help="Extra host path to mount read-write (repeatable)"
        ),
        env: list[str] = typer.Option(
            None,
            "--env",
            metavar="VAR",
            help="Extra host environment variable name to forward (repeatable)",
        ),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
    ) -> None:
        """Experimental: run a command in a sandbox.

        Runs a command or AI harness inside a bubblewrap sandbox. Everything after `--` is the
        command; without one, starts a shell.
        """
        git.check_version()
        if ctx.args:
            hint_tokens = ([ws_name] if ws_name is not None else []) + list(ctx.args)
            hint = " ".join(hint_tokens)
            raise errors.UsageError(
                f"unexpected arguments before `--`; pass commands after `--` (e.g. `ow sandbox -- {hint}`)"
            )
        command = tuple(ctx.meta.get("ow_run_passthrough", ()))
        target = _targets(ws_name, workspace)
        _exit(
            _command_module("sandbox").run(
                plugins,
                command=command,
                workspace=target[0] if target else None,
                profile=profile,
                ro_paths=tuple(ro or ()),
                rw_paths=tuple(rw or ()),
                pass_env=tuple(env or ()),
                dry_run=dry_run,
            )
        )

    # -- repo ------------------------------------------------------------

    repo_app = typer.Typer(
        cls=OwGroup,
        name="repo",
        help="Register, list and remove repos.",
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
    )
    repo_command = partial(repo_app.command, cls=OwCommand)

    @repo_command("add")
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

    @repo_command("ls")
    def repo_ls(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """List the known repos and their remotes."""
        session = _open(workspace, need_workspace=False)
        _announce(session)
        git.check_version()
        _exit(_command_module("repo").ls(session))

    @repo_command("rm")
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
        short_help="Read and write ow settings.",
        invoke_without_command=True,
        add_completion=False,
        rich_markup_mode=None,
    )
    config_command = partial(config_app.command, cls=OwCommand)

    def _config_context(
        ctx: typer.Context, workspace: str | None, global_: bool
    ) -> tuple[str | None, bool]:
        """Merge a verb's own -w/--global with the ones given before it."""
        parent = ctx.obj if isinstance(ctx.obj, dict) else {}
        return (
            workspace or parent.get("workspace"),
            global_ or bool(parent.get("global_")),
        )

    def _open_for_config(
        workspace: str | None,
        *,
        ignore_global: Sequence[str] = (),
        ignore_workspace: Sequence[str] = (),
    ) -> tuple[Session, Path | None]:
        """Open a session for `ow config`, returning `(session, multi_root)` when targeting a multi-stack root."""
        try:
            root, kind = resolve_target_root(workspace)
        except errors.UsageError:
            if workspace is not None:
                raise
            return (
                _open(
                    None,
                    need_workspace=False,
                    ignore_global=ignore_global,
                    ignore_workspace=ignore_workspace,
                ),
                None,
            )
        if kind == "multi":
            stack_paths = stacks.detect_multi_workspace(root)
            eff_plugins = workspace_plugins(stack_paths[0], plugins)[0] if stack_paths else plugins
            sess = open_session(
                None,
                need_workspace=False,
                plugins=eff_plugins,
                cwd=root,
                ignore_global=ignore_global,
                ignore_workspace=ignore_workspace,
            )
            return sess, root
        return (
            _open(
                str(root),
                need_workspace=False,
                ignore_global=ignore_global,
                ignore_workspace=ignore_workspace,
            ),
            None,
        )

    @config_app.callback()
    def config_callback(
        ctx: typer.Context,
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global", help="Read or write the global config"),
    ) -> None:
        """Show every setting with its effective value, its origin and its help."""
        ctx.obj = {"workspace": workspace, "global_": global_}
        if ctx.invoked_subcommand is not None:
            return
        session, multi_root = _open_for_config(workspace)
        _announce(session)
        _exit(
            _command_module("config").show_all(session, global_=global_, multi_root=multi_root)
        )

    @config_command("get")
    def config_get(
        ctx: typer.Context,
        key: str = typer.Argument(
            ..., metavar="KEY", help="Setting key", autocompletion=complete_setting
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        global_: bool = typer.Option(False, "--global", help="Read the global layer"),
    ) -> None:
        """Print one setting's effective value."""
        workspace, global_ = _config_context(ctx, workspace, global_)
        session, multi_root = _open_for_config(workspace)
        _announce(session)
        _exit(
            _command_module("config").get(session, key, global_=global_, multi_root=multi_root)
        )

    @config_command("set")
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
        workspace, global_ = _config_context(ctx, workspace, global_)
        session, multi_root = _open_for_config(workspace)
        _announce(session)
        _exit(
            _command_module("config").set_(
                session, key, _only(values), global_=global_, multi_root=multi_root
            )
        )

    @config_command("unset")
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
        workspace, global_ = _config_context(ctx, workspace, global_)
        session, multi_root = _open_for_config(
            workspace,
            ignore_global=(key,) if global_ else (),
            ignore_workspace=() if global_ else (key,),
        )
        _announce(session)
        _exit(
            _command_module("config").unset(
                session, key, global_=global_, multi_root=multi_root
            )
        )

    app.add_typer(config_app, name="config")

    # -- status ----------------------------------------------------------

    @command("status")
    def status_cmd(
        spec: str = typer.Argument(
            None,
            metavar="SPEC",
            help="Optional external spec (e.g. enterprise#12345 or Runbot URL)",
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        only: list[str] = typer.Option(None, "--only", **only_option),
        fetch: bool = typer.Option(
            False, "--fetch", help="Fetch tracked refs in parallel before showing status"
        ),
        offline: bool = typer.Option(
            False, "--offline", help="Skip external network lookups (GitHub/Runbot)"
        ),
    ) -> None:
        """Show workspace, stack, or external spec status."""
        if spec is not None and workspace is not None:
            raise errors.UsageError("pass either SPEC or -w/--workspace, not both")
        if spec is not None and (only or fetch):
            raise errors.UsageError("--only and --fetch cannot be used with an external SPEC")

        status = _command_module("status")
        if spec is not None:
            git.check_version()
            session = _open(None, need_workspace=False)
            _exit(status.run_spec(session, spec=spec, offline=offline))

        def status_multi(multi_root: Path) -> int:
            if only:
                raise errors.UsageError("--only cannot be used on a multi-stack workspace; pass -w <stack>")
            git.check_version()
            return status.run_multi(plugins, multi_root=multi_root, fetch=fetch, offline=offline)

        root = _single_root(workspace, status_multi)
        _exit(status.run(_checked(root, only), only=_only(only), fetch=fetch, offline=offline))

    # -- rebase ----------------------------------------------------------

    @command("rebase")
    def rebase_cmd(
        base: str = typer.Argument(
            None,
            metavar="BASE",
            help="Change series: rebase onto this base ref and record it as the new base",
        ),
        autostash: bool = typer.Option(
            False, "--autostash", help="Stash modified files before rebasing and pop afterwards"
        ),
        only: list[str] = typer.Option(None, "--only", **only_option),
        no_fetch: bool = typer.Option(
            False, "--no-fetch", help="Do not fetch first; use the cached refs"
        ),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Update each repo on its base, or change series.

        Without BASE, fetch and rebase every repo on its current base (detached
        repos advance). With BASE, move every repo to that base and record it.
        """
        session = _checked(workspace, only)
        _exit(
            _command_module("rebase").run(
                session,
                only=_only(only),
                base=base,
                autostash=autostash,
                no_fetch=no_fetch,
                dry_run=dry_run,
                yes=yes,
            )
        )

    # -- refresh, files --------------------------------------------------

    @command("refresh")
    def refresh_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Rewrite the generated files."""
        refresh = _command_module("refresh")
        root = _single_root(workspace, lambda multi_root: refresh.run_multi(multi_root, plugins))
        _exit(refresh.run(_opened(root)))

    @command("files")
    def files_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        diff: bool = typer.Option(
            False,
            "--diff",
            help="Show the diff of every file that differs instead of the states",
        ),
    ) -> None:
        """Show the generated files and their state."""
        files = _command_module("files")
        root = _single_root(
            workspace, lambda multi_root: files.run_multi(multi_root, plugins, diff=diff)
        )
        _exit(files.run(_opened(root), diff=diff))

    @command("trust")
    def trust_cmd(
        path: str = typer.Argument(
            None,
            metavar="PATH",
            help="Plugin file or directory to trust or untrust (default: this workspace's in-repo plugins)",
        ),
        untrust: bool = typer.Option(
            False, "--untrust", help="Revoke trust for PATH, or for the workspace's plugin files"
        ),
        list_all: bool = typer.Option(
            False, "--list", help="List every trusted plugin file and directory"
        ),
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        yes: bool = typer.Option(False, "-y", "--yes", help="Trust the plugin files found without asking"),
    ) -> None:
        """Trust, untrust, or list where in-repo plugins may run from.

        Trust is by location, like an editor's workspace trust: a trusted plugin file or a
        trusted directory runs whatever code sits there, including code that arrives later
        (e.g. a PR checked out with `ow add`). Without PATH on a terminal, asks whether to
        trust the plugin files found, the workspace root, or its parent directory; with -y,
        trusts the plugin files found. Without a terminal and without -y, refuses (exit 2).
        Then refreshes the workspace.
        """
        _exit(
            _command_module("trust").run(
                plugins,
                path=path,
                workspace=workspace,
                untrust=untrust,
                list_all=list_all,
                yes=yes,
            )
        )

    # -- fleet: ls, cd, shell-init, rm, prune ----------------------------

    @command("ls")
    def ls_cmd() -> None:
        """List the known workspaces and their repos or stacks."""
        git.check_version()
        _exit(_command_module("ls").run(plugins))

    @command("cd")
    def cd_cmd(
        ws: str = typer.Argument(
            None,
            metavar="WS",
            help="Workspace or stack (WS[/STACK]) to print, by name or path (default: current directory)",
            autocompletion=complete_workspace,
        ),
    ) -> None:
        """Print a workspace or stack path."""
        git.check_version()
        try:
            code = _command_module("cd").run(ws)
        except errors.UsageError:
            if ws is not None:
                raise
            # Without WS the only failure is "not inside a workspace", whose generic hint names `-w`, which `cd` lacks.
            raise errors.UsageError(
                "not inside an ow workspace; pass a workspace name or path (`ow cd WS`) or run `ow init`"
            ) from None
        _exit(code)

    @command("shell-init")
    def shell_init_cmd(
        shell: str = typer.Argument(..., metavar="SHELL", help="bash, zsh or fish"),
    ) -> None:
        """Print the shell integration for SHELL."""
        if shell not in ("bash", "zsh", "fish"):
            raise typer.BadParameter(
                f"unsupported shell {shell!r}; expected bash, zsh or fish"
            )
        _exit(_command_module("shell_init").run(shell, app=app))

    @command("rm")
    def rm_cmd(
        ws: str = typer.Argument(
            ...,
            metavar="WS",
            help="Workspace or stack (WS[/STACK]) to remove (e.g. my-ws, review, review/iap)",
            autocompletion=complete_workspace,
        ),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Remove a workspace (or stack) and its worktrees."""
        git.check_version()
        root, kind = resolve_target_root(ws)
        if kind == "multi":
            _exit(_command_module("rm").run_multi(plugins, root, dry_run=dry_run, yes=yes))
        session = _checked(str(root))
        _exit(_command_module("rm").run(session, dry_run=dry_run, yes=yes))

    @command("prune")
    def prune_cmd(
        offline: bool = typer.Option(
            False, "--offline", help="Skip external network lookups (e.g. GitHub merged PRs)"
        ),
        dry_run: bool = typer.Option(False, "-n", "--dry-run", help=DRY_RUN_HELP),
        yes: bool = typer.Option(False, "-y", "--yes", help=YES_HELP),
    ) -> None:
        """Clean up stale state and merged-PR workspaces."""
        git.check_version()
        _exit(
            _command_module("prune").run(
                plugins=plugins, offline=offline, dry_run=dry_run, yes=yes
            )
        )

    return app
