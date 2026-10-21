"""Helpers shared by the ow commands.

`refresh_workspace` and `refresh_after` close a command that changed the workspace; the
`known_repos`/`resolve_base`/`ensure_bare`/`add_worktree` group is the §6 "make a repo usable
here" flow that `ow repo add` and `ow init` both run.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from ow import term
from ow.core import bases, git, refresh
from ow.core.paths import repos_dir
from ow.core.plan import Batch, GitStep, RepoPlan, run_batch
from ow.core.session import Session
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def refresh_workspace(session: Session) -> int:
    """Refresh the session's workspace and print the summary; 1 when an error diagnostic blocked it."""
    assert session.workspace is not None
    projection, written = refresh.refresh(session.workspace, session.plugins)
    refresh.report(projection, written)
    return 1 if projection.blocked else 0


def refresh_after(session: Session) -> int:
    """The one refresh after a git batch: git just moved branches, so re-derive first (spec §7)."""
    return refresh_workspace(session.reload())


def known_repos(plugins: "Plugins") -> dict[str, str]:
    """Registered repo name -> URL, from every `ow_known_repos` answer (§10)."""
    urls: dict[str, str] = {}
    for _plugin, answer in plugins.call("ow_known_repos"):
        for repo in answer or ():
            urls[repo.name] = repo.url
    return urls


def remotes_of(repo_name: str) -> tuple[str, ...]:
    """A bare repo's remote names; `("origin",)` when the bare is missing (§5 validation)."""
    bare = (repos_dir() / f"{repo_name}.git").resolve()
    if not bare.is_dir():
        return ("origin",)
    try:
        output = git.read(["git", "-C", str(bare), "remote"])
    except git.GitFailed:
        return ("origin",)
    return tuple(output.split("\n")) if output else ("origin",)


def accepted_repos(config_path: Path, declared: Mapping[str, object]) -> dict[str, str]:
    """The workspace file's accepted `[repos]` entries, in file order."""
    from ow.core.config import read_workspace

    _layer, accepted, _rejected = read_workspace(config_path, declared, remotes_of=remotes_of)
    return accepted


def render_value(value: object) -> str:
    """A raw config value in TOML spelling, as a dropped-key line prints it (§9)."""
    if value is None:
        return "(unset)"
    if isinstance(value, list):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {render_value(item)}" for key, item in value.items()) + "}"
    return tomli_w.dumps({"v": value}).split("=", 1)[1].strip()


def print_dropped(dropped: Mapping[str, object]) -> None:
    """Print every key a config rewrite dropped, with its old value (§9)."""
    for key, value in dropped.items():
        term.warn(f"dropped {key} = {render_value(value)}")


def origin_has(bare: Path, branch: str) -> bool:
    """Whether `origin` in `bare` has `branch` — the §5 default rule's read."""
    argv = ["git", "-C", str(bare), "ls-remote", "--exit-code", "--heads", "origin", branch]
    return git.run(argv).returncode == 0


def origin_head(bare: Path) -> str | None:
    """The branch `origin/HEAD` points at in `bare`, or None when it is unset."""
    result = git.run(["git", "-C", str(bare), "symbolic-ref", "refs/remotes/origin/HEAD"])
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        return ref[len(prefix):]
    return None


def _parse_base_for(value: str, name: str) -> str:
    """`parse_base` a value, with `name`'s bare remotes as the known set (§5)."""
    remotes = set(remotes_of(name)) or {"origin"}
    return bases.parse_base(value, lambda segment: segment in remotes)


def resolve_base(
    session_or_plugins: Session | "Plugins",
    name: str,
    base: str | None,
    others: Mapping[str, str],
) -> tuple[str, str]:
    """The default base of `name`, and the source it came from (§5).

    `base` goes through `parse_base`; when it is None the §5 default rule runs against `name`'s
    bare. The first argument is the caller's session or plugins: the rule needs only the bare.
    """
    _ = session_or_plugins
    if base is not None:
        return _parse_base_for(base, name), "--base"
    bare = repos_dir() / f"{name}.git"
    inferred = bases.infer_default_base(
        others, lambda branch: origin_has(bare, branch), origin_head(bare)
    )
    if inferred is None:
        raise UsageError(f"no default base for {name}; pass --base")
    return inferred


def base_for(
    session_or_plugins: Session | "Plugins",
    name: str,
    base: str | None,
    recorded: str | None,
    others: Mapping[str, str],
) -> str:
    """The base to add `name` at: `--base`, its recorded `[repos]` value, or the §5 default.

    A `--base` and a recorded value are used as they are; an inferred default prints its source,
    as `# <name>: default base <base> (<source>)`.
    """
    if base is not None:
        return resolve_base(session_or_plugins, name, base, others)[0]
    if recorded is not None:
        return recorded
    resolved, source = resolve_base(session_or_plugins, name, None, others)
    term.out(f"# {name}: default base {resolved} ({source})")
    return resolved


def _run_line(argv: Sequence[str]) -> str:
    """Print a git line just before it runs (additive command, P6 display)."""
    term.out(git.show(argv, None))
    return git.read(argv)


def _default_branch(output: str) -> str | None:
    """The `ref: refs/heads/<b>` target in `ls-remote --symref` output, or None."""
    prefix = "refs/heads/"
    for line in output.splitlines():
        if not line.startswith("ref: "):
            continue
        ref = line[len("ref: "):].split("\t", 1)[0].strip()
        if ref.startswith(prefix) and len(ref) > len(prefix):
            return ref[len(prefix):]
    return None


def ensure_bare(
    plugins: "Plugins",
    name: str,
    url: str,
    remotes_of: Callable[[str], Collection[str]],
    worktree_base: Path | None,
) -> Path:
    """The bare repo of `name`, created with the §6 recipe when it does not exist yet.

    An existing bare runs no git command. The caller resolved `url` already, so the plugin
    catalogue is not read here; every line prints as it runs, with the bare's `$HOME`-relative
    path. `worktree_base` is accepted for the contract's signature and does not affect it.
    """
    _ = plugins, remotes_of, worktree_base
    bare = repos_dir() / f"{name}.git"
    if bare.is_dir():
        return bare
    _run_line(("git", "init", "--bare", str(bare)))
    _run_line(("git", "-C", str(bare), "remote", "add", "origin", url))
    _run_line(("git", "-C", str(bare), "config", "core.logAllRefUpdates", "true"))
    output = _run_line(("git", "-C", str(bare), "ls-remote", "--symref", "origin", "HEAD"))
    default = _default_branch(output)
    if default is None:
        raise UsageError(f"{name}: origin has no default branch; pass a base or check the URL")
    _run_line(("git", "-C", str(bare), "fetch", "--filter=blob:none", "origin", default))
    _run_line(("git", "-C", str(bare), "remote", "set-head", "origin", default))
    return bare


def _has_ref(bare: Path, ref: str) -> bool:
    """Whether `ref` exists in `bare`."""
    argv = ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", ref]
    return git.run(argv).returncode == 0


def _branch_on_one_remote(bare: Path, branch: str) -> tuple[str | None, bool]:
    """The remote holding `branch` when exactly one does, and whether it is cached (§6)."""
    remotes = tuple(remotes_of(bare.stem))
    cached = [remote for remote in remotes if _has_ref(bare, f"refs/remotes/{remote}/{branch}")]
    if len(cached) == 1:
        return cached[0], True
    if len(cached) > 1:
        return None, False
    found: list[str] = []
    for remote in remotes:
        result = git.run(["git", "-C", str(bare), "ls-remote", "--heads", remote, branch])
        if result.returncode == 0 and result.stdout.strip():
            found.append(remote)
    if len(found) == 1:
        return found[0], False
    return None, False


def _fetch_refspec(bare: Path, base: str) -> tuple[str, str]:
    """The (remote, branch) a base fetch names: the base's own remote when it has one."""
    if "/" in base:
        head, rest = base.split("/", 1)
        if head in tuple(remotes_of(bare.stem)):
            return head, rest
    return "origin", base.split("/", 1)[-1]


def add_worktree(
    bare: Path,
    worktree: Path,
    *,
    branch: str | None,
    base: str,
    remotes_of: Callable[[str], Collection[str]],
) -> tuple[GitStep, ...]:
    """The §6 steps that add `worktree` to `bare` at `base`, detached or on `branch`.

    The steps are returned, not run: the caller prints each as it runs it (`run_steps`), so an
    additive command shows only the commands it actually executes.
    """
    _ = remotes_of
    target = str(Path(worktree).resolve())
    fetch_remote, base_branch = _fetch_refspec(bare, base)
    steps = [GitStep(("git", "-C", str(bare), "fetch", fetch_remote, base_branch))]
    if branch is None:
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "add", "--detach", target, base)))
        return tuple(steps)
    if _has_ref(bare, f"refs/heads/{branch}"):
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "add", target, branch)))
        return tuple(steps)
    remote, cached = _branch_on_one_remote(bare, branch)
    if remote is not None:
        if not cached:
            steps.append(GitStep(("git", "-C", str(bare), "fetch", remote, branch)))
        steps.append(
            GitStep(
                (
                    "git", "-C", str(bare), "worktree", "add", "--track", "-b", branch, target,
                    f"{remote}/{branch}",
                )
            )
        )
    else:
        steps.append(
            GitStep(
                ("git", "-C", str(bare), "worktree", "add", "--no-track", "-b", branch, target, base)
            )
        )
    steps.append(GitStep(("git", "-C", target, "config", f"branch.{branch}.ow-base", base)))
    return tuple(steps)


def run_steps(name: str, steps: Sequence[GitStep], root: Path | None = None) -> int:
    """Print each step as it runs, one after the other; 1 when one failed (P6 display)."""
    outcome = run_batch(
        Batch(root=root, repos=(RepoPlan(repo=name, steps=tuple(steps)),)),
        prompt=False,
        dry_run=False,
        yes=True,
    )
    return outcome.exit_code
