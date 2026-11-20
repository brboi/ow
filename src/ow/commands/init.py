"""`ow init` — create, adopt or repair the workspace at a directory."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._repos import add_worktree, base_for, ensure_bare, repair_bare, resolve_base
from ow.commands._shared import (
    known_repos,
    print_dropped,
    refresh_workspace,
    remotes_of,
    run_steps,
)
from ow.core import bases, config, git, index
from ow.core.config import Layer
from ow.core.paths import repos_dir
from ow.core.session import open_session
from ow.core.workspace import derive_repos
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import RepoLocation


def run(
    plugins: "Plugins",
    directory: Path,
    repos: Sequence[str],
    *,
    base: str | None,
    branch: str | None,
) -> int:
    """Bring `directory` to a full ow workspace; returns the exit code (1 when a step failed)."""
    root = Path(directory).resolve()
    config_path = root / ".ow" / "config.toml"
    create = not config_path.exists()

    names = list(dict.fromkeys(repos))
    catalog = known_repos(plugins)
    existing = derive_repos(root)

    # Validate before touching the filesystem: a usage error must leave no empty directory.
    for name in names:
        if not (repos_dir() / f"{name}.git").is_dir() and name not in catalog:
            raise UsageError(f"unknown repo {name}; pass a URL: ow repo add {name} <url>")
    target_remotes = _target_remotes(names, existing)
    _validate(base, target_remotes)
    _validate(branch, target_remotes)

    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    if create:
        declared = {setting.key: setting for setting in plugins.settings()}
        workspace_layer = Layer(values={}, unknown={}, wrong_type=())
        accepted: dict[str, str] = {}
        rejected: dict[str, str] = {}
        locations = existing
    else:
        session = open_session(str(root), need_workspace=False, plugins=plugins)
        declared = dict(session.declared)
        workspace_layer = session.workspace_layer
        accepted = dict(session.accepted_bases)
        rejected = dict(session.rejected_bases)
        locations = session.locations

    # Repair: `worktree repair` relinks a moved worktree; a `forgotten` one cannot be repaired
    # and fails at the end, after the rest of the run.
    failed = False
    for location in locations:
        if location.problem == "prunable":
            argv = ("git", "-C", str(location.path), "worktree", "repair")
            term.out(git.show(argv, root))
            if git.run(argv).returncode != 0:
                failed = True
        elif location.problem == "forgotten":
            term.error(
                f"{location.name}: worktree forgotten by {git.show((str(location.bare),), root)}; "
                "move the directory away and re-run `ow init`"
            )
            failed = True
    if locations:
        locations = derive_repos(root, accepted)

    # Adopt: record each repo already here, repairing a bare ow 2.x left without a fetch
    # refspec or `origin/HEAD`.
    merged: dict[str, str] = dict(accepted)
    for location in locations:
        repair_bare(location.bare)
        if location.name in merged:
            continue
        resolved, source = resolve_base(location.name, base, accepted, agreement_first=True)
        merged[location.name] = resolved
        term.out(f"# {location.name}: default base {resolved} ({source})")

    # Add: each `<repo>` of the argument list, then every `[repos]` entry whose worktree is gone.
    present = {location.name: location for location in locations}
    to_add = list(names) + [name for name in merged if name not in present and name not in names]
    summary: list[str] = []
    for name in to_add:
        location = present.get(name)
        if location is not None:
            if branch is not None and location.branch != branch:
                summary.append(f"# {name}: present; run `ow switch {branch}`")
            continue
        bare = repos_dir() / f"{name}.git"
        if not bare.is_dir() and name not in catalog:
            term.warn(f"{name}: no bare repo; run `ow repo add {name} <url>`")
            continue
        if bare.is_dir():
            # A bare already here may be an adopted ow 2.x one: repair it exactly as an adopted
            # location is repaired, so its ref logs and `origin/HEAD` exist before the add.
            repair_bare(bare)
        else:
            ensure_bare(name, catalog.get(name, ""))
        others = {repo: value for repo, value in merged.items() if repo != name}
        resolved = base_for(name, base, merged.get(name), others)
        steps = add_worktree(bare, root / name, branch=branch, base=resolved)
        if run_steps(name, steps, root=root):
            failed = True
            continue
        merged[name] = resolved
        summary.append(f"added {name} (base {resolved})")

    # Write: a rewrite drops every key ow does not know, so it only happens when it would
    # change something. The create path always writes.
    values = dict(workspace_layer.values)
    if (
        create
        or merged != accepted
        or bool(workspace_layer.unknown)
        or bool(workspace_layer.wrong_type)
        or bool(rejected)
    ):
        dropped = config.write_workspace(
            config_path, merged, values, declared=declared, remotes_of=remotes_of
        )
        print_dropped(dropped)

    index.add(root)
    fresh = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    if create and fresh.workspace is not None:
        plugins.call("ow_workspace_created", fresh.workspace)
    code = refresh_workspace(fresh)
    for line in summary:
        term.out(line)
    return 1 if failed or code else 0


def _target_remotes(names: Sequence[str], locations: Sequence[RepoLocation]) -> set[str]:
    """The union of the target repos' remotes, plus `origin` for one not registered yet."""
    remotes = {"origin"}
    for name in list(names) + [location.name for location in locations]:
        remotes.update(remotes_of(name))
    return remotes


def _validate(value: str | None, remotes: set[str]) -> None:
    """`parse_base` an argument so an invalid ref fails before any git command."""
    if value is None:
        return
    bases.parse_base(value, lambda segment: segment in remotes)
