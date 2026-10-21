"""`ow init` — create, adopt or repair the workspace at a directory (§6, §13).

`run` is re-runnable like `git init`: it repairs prunable worktrees, adopts the default bases of
the repos already in the directory, adds each `<repo>` of the argument list, writes
`.ow/config.toml`, remembers the root in the index and refreshes. `ow_workspace_created` fires on
the create path only, before the first refresh. The last stdout lines are, in argument order,
`# <repo>: present; run `ow switch <branch>`` for a repo already there under `--branch`, and
`added <repo> (base <base>)` for one this run added.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._shared import (
    accepted_repos,
    add_worktree,
    base_for,
    ensure_bare,
    known_repos,
    origin_has,
    origin_head,
    print_dropped,
    remotes_of,
    refresh_workspace,
    run_steps,
)
from ow.core import bases, config, git, index
from ow.core.config import Layer
from ow.core.paths import global_config, repos_dir
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
    root = Path(directory)
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    config_path = root / ".ow" / "config.toml"
    create = not config_path.exists()

    if create:
        # No session can be opened for a directory without `.ow/config.toml`, so build what a
        # session holds: the declared settings, the global layer and the derived repos.
        declared = {setting.key: setting for setting in plugins.settings()}
        config.read_global(global_config(), declared)
        workspace_layer = Layer(values={}, unknown={}, wrong_type=())
        accepted: dict[str, str] = {}
        rejected: dict[str, str] = {}
        locations = derive_repos(root)
    else:
        session = open_session(str(root), need_workspace=False, plugins=plugins)
        declared = dict(session.declared)
        workspace_layer = session.workspace_layer
        accepted = accepted_repos(config_path, declared)
        rejected = dict(session.rejected_bases)
        locations = session.locations

    target_remotes = _target_remotes(repos, locations)
    _validate(base, target_remotes)
    _validate(branch, target_remotes)

    # Repair (§6): a moved worktree is `prunable` to its bare, and `worktree repair` fixes the
    # link. A `forgotten` one (already pruned from the bare) cannot be repaired, so it fails at
    # the end — after the rest of the run, which still has work to do.
    failed = False
    for location in locations:
        if location.problem == "prunable":
            argv = ("git", "-C", str(location.path), "worktree", "repair")
            term.out(git.show(argv, root))
            if git.run(argv).returncode != 0:
                failed = True
        elif location.problem == "forgotten":
            term.error(
                f"{location.name}: worktree forgotten by {_display(location.bare)}; "
                "move the directory away and re-run `ow init`"
            )
            failed = True
    if locations:
        locations = derive_repos(root, accepted)

    # Adopt (§13): every repo already in the directory gets its default base recorded.
    merged: dict[str, str] = dict(accepted)
    for location in locations:
        if location.name in merged:
            continue
        resolved, source = _adopt_base(location, base, accepted)
        merged[location.name] = resolved
        term.out(f"# {location.name}: default base {resolved} ({source})")

    # Add (§6): each `<repo>` of the argument list that is not there yet.
    summary: list[str] = []
    for name in repos:
        if any(location.name == name for location in locations):
            if branch is not None:
                summary.append(f"# {name}: present; run `ow switch {branch}`")
            continue
        bare = repos_dir() / f"{name}.git"
        catalog = known_repos(plugins)
        if not bare.is_dir() and name not in catalog:
            raise UsageError(f"unknown repo {name}; pass a URL: ow repo add {name} <url>")
        ensure_bare(plugins, name, catalog.get(name, ""), remotes_of, root)
        others = {repo: value for repo, value in merged.items() if repo != name}
        resolved = base_for(plugins, name, base, merged.get(name), others)
        steps = add_worktree(
            bare, root / name, branch=branch, base=resolved, remotes_of=remotes_of
        )
        if run_steps(name, steps, root=root):
            failed = True
            continue
        merged[name] = resolved
        summary.append(f"added {name} (base {resolved})")

    # Write (§9, §13): a rewrite drops every key ow does not know, so it only happens when it
    # would change something. The create path always writes.
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


def _adopt_base(
    location: RepoLocation, base: str | None, accepted: Mapping[str, str]
) -> tuple[str, str]:
    """An adopted repo's default base (§13): the agreement rule, else `--base`, else `origin/HEAD`."""
    bare = location.bare
    others = {repo: value for repo, value in accepted.items() if repo != location.name}
    agreement = bases.infer_default_base(others, lambda branch: origin_has(bare, branch), None)
    if agreement is not None:
        return agreement
    if base is not None:
        remotes = set(remotes_of(location.name)) or {"origin"}
        return bases.parse_base(base, lambda segment: segment in remotes), "--base"
    head = bases.infer_default_base(
        {}, lambda branch: origin_has(bare, branch), origin_head(bare)
    )
    if head is not None:
        return head
    raise UsageError(f"no default base for {location.name}; pass --base")


def _target_remotes(names: Sequence[str], locations: Sequence[RepoLocation]) -> set[str]:
    """The union of the target repos' remotes, plus `origin` for one not registered yet (§5)."""
    remotes = {"origin"}
    for name in list(names) + [location.name for location in locations]:
        remotes.update(remotes_of(name))
    return remotes


def _validate(value: str | None, remotes: set[str]) -> None:
    """`parse_base` an argument so an invalid ref fails before any git command (§5)."""
    if value is None:
        return
    bases.parse_base(value, lambda segment: segment in remotes)


def _display(path: Path) -> str:
    """A path as the git lines print it: `~/...` under $HOME, else quoted."""
    home = Path.home()
    try:
        return "~/" + shlex.quote(str(path.relative_to(home)))
    except ValueError:
        return shlex.quote(str(path))
