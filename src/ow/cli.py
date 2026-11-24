"""CLI application builder for ow.

Every Typer signature lives here (a `commands/` module never imports `ow.cli`).
"""
from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
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


class _RunCommand(TyperCommand):
    """Capture everything after `--` as passthrough args before Click parses WS/options."""

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        if "--" in args:
            idx = args.index("--")
            ctx.meta["ow_run_passthrough"] = tuple(args[idx + 1 :])
            args = args[:idx]
        else:
            ctx.meta["ow_run_passthrough"] = ()
        return super().parse_args(ctx, args)


class OwGroup(TyperGroup):
    """Custom Typer group that handles no_args_is_help with exit 0 and OwError exceptions."""

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        """Override to handle bare 'ow' (no args) by printing help and exiting with 0.

        A completion request parses with empty args too, so fall through when parsing.
        """
        if not args and not ctx.resilient_parsing:
            term.out(ctx.get_help())
            ctx.exit(0)
        return super().parse_args(ctx, args)

    def invoke(self, ctx: typer.Context) -> None:
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

    def complete_setting(ctx: Any, incomplete: str) -> list[str]:
        return [key for key in _setting_keys() if key.startswith(incomplete)]

    workspace_option = dict(metavar="WS", help=WORKSPACE_HELP, autocompletion=complete_workspace)
    only_option = dict(metavar="REPO", help=ONLY_HELP, autocompletion=complete_repo)

    # -- init ------------------------------------------------------------

    @app.command("init")
    def init_cmd(
        specs: list[str] = typer.Argument(
            None,
            metavar="SPEC",
            help="Spec(s): @18.0, enterprise#12345, a=community#1,enterprise#2, voip",
        ),
        branch: str = typer.Option(
            None, "--branch", metavar="BRANCH", help="Create or check out this branch in every added repo"
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
                branch=branch,
                as_name=as_name,
                dry_run=dry_run,
                yes=yes,
            )
        )

    # -- add -------------------------------------------------------------

    @app.command("add")
    def add_cmd(
        specs: list[str] = typer.Argument(
            ...,
            metavar="SPEC",
            help="Repo, PR, or named stack spec to add inside the current workspace",
        ),
        branch: str = typer.Option(
            None, "--branch", metavar="BRANCH", help="Create or check out this branch in every added repo"
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
                branch=branch,
                as_name=as_name,
                dry_run=dry_run,
                yes=yes,
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
        """Run a workspace (foreground or detached with -d)."""
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

    @app.command("stop")
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

    @app.command("logs")
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

    @app.command(
        "exec",
        cls=_RunCommand,
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
        """Run a command or shell in a stack's virtualenv."""
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

    @app.command(
        "sandbox",
        cls=_RunCommand,
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": False,
            "allow_interspersed_args": False,
        },
    )
    def sandbox_cmd(
        ctx: typer.Context,
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
        profile: str = typer.Option(
            None,
            "--profile",
            metavar="NAME",
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
        """Run a command or AI harness inside a bubblewrap sandbox."""
        git.check_version()
        passthrough = tuple(ctx.meta.get("ow_run_passthrough", ()))
        command = tuple(ctx.args) + passthrough
        _exit(
            _command_module("sandbox").run(
                plugins,
                command=command,
                workspace=workspace,
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

    @config_app.command("get")
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
        workspace, global_ = _config_context(ctx, workspace, global_)
        session, multi_root = _open_for_config(workspace)
        _announce(session)
        _exit(
            _command_module("config").set_(
                session, key, _only(values), global_=global_, multi_root=multi_root
            )
        )

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

    @app.command("status")
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

    # -- retarget --------------------------------------------------------

    @app.command("retarget")
    def retarget_cmd(
        target_base: str = typer.Argument(
            None,
            metavar="BASE",
            help="New base ref to retarget onto (shorthand for --base)",
        ),
        base: str = typer.Option(
            None,
            "--base",
            metavar="REF",
            help="Rebase onto this base ref and record it as the new base",
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
        """Guarded replay of each repo onto its base (or a new base ref)."""
        if target_base is not None and base is not None:
            raise errors.UsageError("pass either positional BASE or --base, not both")
        session = _checked(workspace, only)
        _exit(
            _command_module("retarget").run(
                session,
                only=_only(only),
                base=target_base if target_base is not None else base,
                autostash=autostash,
                no_fetch=no_fetch,
                dry_run=dry_run,
                yes=yes,
            )
        )

    # -- refresh, files --------------------------------------------------

    @app.command("refresh")
    def refresh_cmd(
        workspace: str = typer.Option(None, "-w", "--workspace", **workspace_option),
    ) -> None:
        """Rewrite the generated files."""
        refresh = _command_module("refresh")
        root = _single_root(workspace, lambda multi_root: refresh.run_multi(multi_root, plugins))
        _exit(refresh.run(_opened(root)))

    @app.command("files")
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

    @app.command("trust")
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
    ) -> None:
        """Trust, untrust, or list where in-repo plugins may run from.

        Trust is by location, like an editor's workspace trust: a trusted plugin file or a
        trusted directory runs whatever code sits there, including code that arrives later
        (e.g. a PR checked out with `ow add`). Without PATH on a terminal, asks whether to
        trust the plugin files found, the workspace root, or its parent directory; without a
        terminal, trusts the plugin files found. Then refreshes the workspace.
        """
        _exit(
            _command_module("trust").run(
                plugins,
                path=path,
                workspace=workspace,
                untrust=untrust,
                list_all=list_all,
            )
        )

    # -- fleet: ls, cd, shell-init, rm, prune ----------------------------

    @app.command("ls")
    def ls_cmd() -> None:
        """List the known workspaces and their repos or stacks."""
        git.check_version()
        _exit(_command_module("ls").run(plugins))

    @app.command("cd")
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
        _exit(_command_module("cd").run(ws))

    @app.command("shell-init")
    def shell_init_cmd(
        shell: str = typer.Argument(..., metavar="SHELL", help="bash, zsh or fish"),
    ) -> None:
        """Print the shell integration for SHELL."""
        if shell not in ("bash", "zsh", "fish"):
            raise typer.BadParameter(
                f"unsupported shell {shell!r}; expected bash, zsh or fish"
            )
        _exit(_command_module("shell_init").run(shell, app=app))

    @app.command("rm")
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

    @app.command("prune")
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
