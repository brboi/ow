"""Creating or repairing a workspace, here or in ./NAME.

`ow init` is re-runnable: pointed at a directory that is already a
workspace, it repairs it — recreates whatever worktree went missing,
seeds whatever `.local` file is still absent, and renders whatever
generated file is stale — without ever touching a repo or a branch spec
that is already there and correct. Pointed at a new directory, it creates
one, interactively when a human is asking, from flags and `-c` otherwise.
"""

import re
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

from rich.prompt import Prompt

from ow.utils import index, paths
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    load_global_config,
    load_workspace_config,
    parse_branch_spec,
    write_workspace_config,
)
from ow.utils.display import confirm, console, err_console
from ow.utils.drift import print_drift_warning
from ow.utils.materialize import materialize_missing, seed_local_files
from ow.utils.migration import commit_migration, plan_migration
from ow.utils.options import MiseOverrides, OdooOverrides
from ow.utils.workspace import refresh_workspace, require_mise

MARKER = Path(".ow") / "config.toml"

# ---------------------------------------------------------------------------
# Internal helpers for cmd_init
# ---------------------------------------------------------------------------


def _resolve_target(
    config: Config,
    name: str | None,
    repos: dict[str, BranchSpec] | None,
    configuration: str | None,
    *,
    parent: Path | None = None,
) -> tuple[WorkspaceConfig | None, Path, bool]:
    """Validate CLI inputs, resolve the target directory, and say whether it exists.

    Returns `(source_ws, ws_dir, existing)`. `source_ws` is `-c`'s source,
    read-only and never mutated. Existence-dependent refusals (a
    conflicting `-r`, or `-c` pointed at a workspace that is already one)
    are the caller's business once `existing` is known.
    """
    known_aliases = list(config.remotes.keys())
    if repos is not None:
        unknown = [alias for alias in repos if alias not in known_aliases]
        if unknown:
            avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
            print(f"Error: unknown repo alias(es): {', '.join(unknown)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

    source_ws: WorkspaceConfig | None = None
    if configuration is not None:
        src_path = Path(configuration)
        src_config_file = src_path / MARKER if src_path.is_dir() else src_path
        if not src_config_file.exists():
            print(f"Error: configuration file not found: {src_config_file}", file=sys.stderr)
            sys.exit(1)
        try:
            source_ws = load_workspace_config(src_config_file)
        except (OSError, ValueError) as exc:
            print(f"Error: could not load {src_config_file}: {exc}", file=sys.stderr)
            sys.exit(1)
        for alias in source_ws.repos:
            if alias not in known_aliases:
                avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
                print(f"Error: configuration references repo '{alias}' but it's not defined in [remotes]", file=sys.stderr)
                print(f"  Available remotes: {avail}", file=sys.stderr)
                sys.exit(1)

    base_dir = parent or Path.cwd()
    if name is None:
        # No argument means "here", like `git init`. The charset rule below
        # does not apply: ow is not naming this directory, it is standing in
        # one the user already named.
        ws_dir = base_dir
    else:
        stripped = name.strip()
        if not stripped or not re.match(r'^[a-zA-Z0-9_-]+$', stripped):
            print("Error: name must be alphanumeric with hyphens and underscores only.", file=sys.stderr)
            sys.exit(1)
        ws_dir = base_dir / stripped

    existing = (ws_dir / MARKER).exists()
    return source_ws, ws_dir, existing


def _preselection(
    source_ws: WorkspaceConfig | None, repos: dict[str, BranchSpec] | None
) -> dict[str, BranchSpec]:
    """Everything the caller already decided, before any question is asked."""
    chosen_repos: dict[str, BranchSpec] = dict(source_ws.repos) if source_ws is not None else {}
    if repos:
        chosen_repos.update(repos)
    return chosen_repos


def _workspace_config_from_flags(
    source_ws: WorkspaceConfig | None, repos: dict[str, BranchSpec] | None
) -> WorkspaceConfig:
    """Build a new workspace's config without asking anything.

    Used when stdin is not a terminal. A repo-less workspace is legitimate
    here: nothing given at all is not an error, it is a generic workspace.
    """
    chosen_repos = _preselection(source_ws, repos)
    odoo = source_ws.odoo if source_ws is not None else OdooOverrides()
    mise = source_ws.mise if source_ws is not None else MiseOverrides()
    return WorkspaceConfig(repos=chosen_repos, odoo=odoo, mise=mise)


def _ask_multi(title: str, items: list[str], preselected: set[str]) -> list[str]:
    """Numbered multi-select.

    Prints the list with ``*`` marking preselected items, then asks for a
    comma-separated answer of names and/or 1-based indices. Empty answer
    keeps the default; the literal string ``none`` (case-insensitive) or
    ``-`` selects nothing. Re-asks on an unknown token.
    """
    default_indices = [str(i + 1) for i, item in enumerate(items) if item in preselected]
    default_str = ",".join(default_indices)

    while True:
        console.print(f"[bold]{title}[/]")
        for i, item in enumerate(items, 1):
            marker = "*" if item in preselected else " "
            console.print(f"  {i} {marker} {item}")

        answer = Prompt.ask(
            f'Select [{default_str}] (comma-separated, "none" for none)',
            default=default_str,
            console=console,
        )

        answer = answer.strip()
        if not answer or answer.lower() in ("none", "-"):
            return []

        tokens = [t.strip() for t in answer.split(",") if t.strip()]
        selected: list[str] = []
        unknown: list[str] = []
        for token in tokens:
            if token in items:
                if token not in selected:
                    selected.append(token)
                continue
            try:
                idx = int(token)
                if 1 <= idx <= len(items):
                    name = items[idx - 1]
                    if name not in selected:
                        selected.append(name)
                    continue
            except ValueError:
                pass
            unknown.append(token)

        if unknown:
            for token in unknown:
                err_console.print(f"unknown: {token}")
            continue

        return selected


def _ask_spec(alias: str, default: str) -> BranchSpec:
    """Prompt for a branch spec, re-asking while parse_branch_spec raises."""
    while True:
        answer = Prompt.ask(
            f"{alias} branch spec",
            default=default,
            console=console,
        )
        answer = answer.strip() or default
        try:
            return parse_branch_spec(answer)
        except ValueError as e:
            err_console.print(f"Error: invalid branch spec '{answer}': {e}")


def _gather_workspace_config_interactive(
    config: Config,
    source_ws: WorkspaceConfig | None,
    repos: dict[str, BranchSpec] | None,
) -> WorkspaceConfig | None:
    """Run the interactive questionnaire to build a new workspace's config.

    Returns None if the user cancelled. Nothing is preselected beyond what
    `-c`/`-r` already named: a bare `ow init` at a terminal offers every
    known alias unchecked, never guessing `community` for the user.
    """
    final_repos = _preselection(source_ws, repos)
    _check_duplicate_branches(final_repos)

    known_aliases = list(config.remotes.keys())
    try:
        if known_aliases:
            selected_aliases = _ask_multi("Repos", known_aliases, set(final_repos.keys()))
            for alias in selected_aliases:
                if alias not in final_repos:
                    final_repos[alias] = _ask_spec(alias, "master")
    except KeyboardInterrupt:
        err_console.print("Aborted.")
        return None

    odoo = source_ws.odoo if source_ws is not None else OdooOverrides()
    mise = source_ws.mise if source_ws is not None else MiseOverrides()
    return WorkspaceConfig(repos=final_repos, odoo=odoo, mise=mise)


def _check_duplicate_branches(
    new_repos: dict[str, BranchSpec], *, ignore: Path | None = None
) -> None:
    """Abort if a repo alias would reuse the local branch of a known workspace.

    Only local branches (the part after `..`) can collide — source branches
    are shared freely, git only objects to two worktrees on one local branch.

    Best-effort by design: it reads index.known_workspaces(), so a workspace
    ow has never resolved is invisible here and slips through. `ignore` is
    the workspace being repaired, if any: comparing its own declared repos
    against itself is not a collision.
    """
    ignore_resolved = ignore.resolve() if ignore is not None else None
    for existing_ws_dir in index.known_workspaces():
        if ignore_resolved is not None and existing_ws_dir.resolve() == ignore_resolved:
            continue
        try:
            existing = load_workspace_config(existing_ws_dir / MARKER)
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            continue
        for alias, new_spec in new_repos.items():
            if alias not in existing.repos:
                continue
            existing_spec = existing.repos[alias]
            new_target = new_spec.local_branch
            existing_target = existing_spec.local_branch
            if new_target and existing_target and new_target == existing_target:
                print(f"Error: workspace '{existing_ws_dir.name}' already uses {alias}:{existing_spec.to_spec_str()}", file=sys.stderr)
                print(f"  Target branch '{new_target}' is already in use. Each target branch must be unique.", file=sys.stderr)
                print(f"  Use -r {alias}:SPEC to override the duplicated repo.", file=sys.stderr)
                sys.exit(1)


# ---------------------------------------------------------------------------
# Command: init
# ---------------------------------------------------------------------------


def cmd_init(
    config: Config,
    name: str | None = None,
    repos: dict[str, BranchSpec] | None = None,
    configuration: str | None = None,
    *,
    parent: Path | None = None,
    yes: bool = False,
) -> None:
    """Create a workspace in the current directory, or in ./NAME — or repair one already there.

    name: directory to create/repair under the current one; without it, "here"
    repos: dict of alias -> BranchSpec, explicit overrides or additions
    configuration: path to an existing workspace config to duplicate (new targets only)
    parent: directory to create the workspace in (default: cwd)
    yes: skip the confirmation prompt
    """
    source_ws, ws_dir, existing = _resolve_target(config, name, repos, configuration, parent=parent)
    ow_config_path = ws_dir / MARKER

    if existing and configuration is not None:
        err_console.print(f"Error: {ws_dir} is already a workspace; -c only creates a new one.", markup=False)
        err_console.print(f"       edit {ow_config_path} directly, or run `ow init` there to repair it.", markup=False)
        sys.exit(1)

    if existing:
        try:
            existing_ws = load_workspace_config(ow_config_path)
        except (OSError, ValueError) as exc:
            err_console.print(f"Error: could not load {ow_config_path}: {exc}", markup=False)
            sys.exit(1)

        conflicts = [
            alias for alias, spec in (repos or {}).items()
            if alias in existing_ws.repos and existing_ws.repos[alias] != spec
        ]
        if conflicts:
            err_console.print(
                "Error: -r conflicts with the existing workspace: " + ", ".join(sorted(conflicts)),
                markup=False,
            )
            err_console.print("       run `ow switch` to change a repo's branch instead.", markup=False)
            sys.exit(1)

        merged_repos = dict(existing_ws.repos)
        for alias, spec in (repos or {}).items():
            merged_repos.setdefault(alias, spec)
        ws = replace(existing_ws, repos=merged_repos)
        _check_duplicate_branches(ws.repos, ignore=ws_dir)
    else:
        interactive = sys.stdin.isatty() and not yes
        if interactive:
            ws = _gather_workspace_config_interactive(config, source_ws, repos)
            if ws is None:
                sys.exit(2)
        else:
            ws = _workspace_config_from_flags(source_ws, repos)
            _check_duplicate_branches(ws.repos)

    try:
        require_mise()
    except ValueError as exc:
        err_console.print(f"Error: {exc}", markup=False)
        sys.exit(1)

    verb = "repaired" if existing else "created"
    console.print(f"\nWorkspace '{ws_dir.name}' will be {verb} in {ws_dir} with:")
    if ws.repos:
        for alias, spec in ws.repos.items():
            console.print(f"  {alias}: {spec.to_spec_str()}")
    else:
        console.print("  (no repos declared)")

    if not existing and sys.stdin.isatty() and not yes:
        if not confirm():
            console.print("Aborted.")
            sys.exit(2)

    ws_dir.mkdir(parents=True, exist_ok=True)

    if config.version == 1 or ws.version == 1:
        migration_plan = plan_migration(config, ws, ws_dir)
        if migration_plan.errors:
            for line in migration_plan.errors:
                err_console.print(f"Error: {line}", markup=False)
            sys.exit(1)
        commit_migration(migration_plan)
        for line in migration_plan.warnings:
            err_console.print(line, markup=False)
        if config.version == 1:
            config = load_global_config()
        if ws.version == 1:
            ws = load_workspace_config(ow_config_path)
    else:
        write_workspace_config(ow_config_path, ws)

    # The config is the truth from this point on; a repo or a render
    # failing below must still leave it exactly as intended, so a retry
    # (another `ow init` here) has something meaningful to repair.
    materialize_result = materialize_missing(ws, config, ws_dir)
    print_drift_warning(list(materialize_result.drifted))
    for alias, error in sorted(materialize_result.errors.items()):
        err_console.print(f"Error: {alias}: {error}", markup=False)

    seed_conflict = None
    seed_errors: tuple[str, ...] = ()
    if ".local" in ws.repos:
        seed_conflict = "'.local' is a declared worktree; ow will not seed local files into it"
        err_console.print(f"Error: {seed_conflict}", markup=False)
    else:
        seed_result = seed_local_files(paths.local_dir(), ws_dir)
        seed_errors = seed_result.errors
        for error in seed_errors:
            err_console.print(f"Error: seeding .local: {error}", markup=False)

    render_result = None
    if not materialize_result.errors and not seed_errors and seed_conflict is None:
        render_result = refresh_workspace(config, ws, ws_dir, trust=True)
        for path in render_result.wrote:
            console.print(f"wrote {path}")
        for path in render_result.updated:
            console.print(f"updated {path}")
        for path in render_result.adopted:
            console.print(f"adopted {path}")
        if render_result.yours:
            console.print("yours, left alone: " + ", ".join(render_result.yours))
        for warning in render_result.warnings:
            err_console.print(warning, markup=False)
        if render_result.failed:
            for line in render_result.errors:
                err_console.print(f"Error: {line}", markup=False)

    failed = (
        bool(materialize_result.errors)
        or bool(seed_errors)
        or seed_conflict is not None
        or render_result is None
        or render_result.failed
    )

    console.print(f"\nWorkspace config: {ow_config_path}")
    if failed:
        console.print(f"\nWorkspace '{ws_dir.name}' {verb} with errors. Fix issues and run: ow init")
        sys.exit(1)

    index.remember(ws_dir)
    console.print(f"\nWorkspace '{ws_dir.name}' {verb}.")
