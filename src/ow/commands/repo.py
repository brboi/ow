"""`ow repo add`, `ow repo ls` and `ow repo rm`."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ow import term
from ow.commands._repos import add_worktree, base_for, ensure_bare, parse_base_for, repair_bare
from ow.commands._shared import (
    known_repos,
    print_dropped,
    refresh_after,
    refresh_workspace,
    remotes_of,
    run_steps,
)
from ow.core import config, git
from ow.core.facts import RepoFacts, gather_facts
from ow.core.paths import repos_dir
from ow.core.plan import Batch, GitStep, RepoPlan, Step, run_batch
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes import preflight
from ow.recipes.removal import branch_removal, orphan_commits


def add(session: Session, name: str, url: str | None, *, base: str | None) -> int:
    """Register `name` and, inside a workspace, add its worktree at its default base.

    `--base` is parsed before any git command, so a bad base creates no bare.
    """
    bare = repos_dir() / f"{name}.git"
    url = _url_for(session, name, url, registered=bare.is_dir())
    if base is not None:
        parse_base_for(base, name)
    if bare.is_dir() and url:
        registered = _origin_url(bare)
        if registered is not None and registered != url:
            term.out(f"# {name}: registered origin {registered} differs; keeping the registered one")

    workspace = session.workspace
    if workspace is None:
        ensure_bare(name, url)
        return 0

    accepted = dict(session.accepted_bases)
    others = {repo: value for repo, value in accepted.items() if repo != name}
    if any(location.name == name for location in session.locations):
        if base is None:
            return 0
        resolved = base_for(name, base, accepted.get(name), others)
        return _record(session, accepted, name, resolved)

    ensure_bare(name, url)
    repair_bare(bare)
    resolved = base_for(name, base, accepted.get(name), others)
    steps = add_worktree(bare, workspace.root / name, branch=None, base=resolved)
    code = run_steps(name, steps, root=workspace.root)
    if code:
        return code
    return _record(session, accepted, name, resolved)


def ls(session: Session) -> int:
    """One `<name>: <url>` line per bare repo, `(in this workspace)` on the ones used here.

    A bare with no `origin` prints `(no origin)`. No bare repos prints nothing; the exit code is
    0 either way.
    """
    directory = repos_dir()
    if not directory.is_dir():
        return 0
    used = (
        {repo.name for repo in session.workspace.repos} if session.workspace is not None else set()
    )
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        if not entry.is_dir():
            continue
        name = entry.stem if entry.name.endswith(".git") else entry.name
        url = _origin_url(entry)
        line = f"{name}: {url}" if url else f"{name}: (no origin)"
        if name in used:
            line += " (in this workspace)"
        term.out(line)
    return 0


def rm(session: Session, name: str, *, dry_run: bool, yes: bool) -> int:
    """Remove `name`'s worktree from this workspace: the plan, the prompt, the rewrite.

    A dirty worktree, or a detached HEAD holding commits no remote has, refuses before the prompt.
    """
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")
    location = next((entry for entry in session.locations if entry.name == name), None)
    if location is None:
        return _drop_missing(session, name)

    facts = gather_facts(location)
    problems = _refusals(facts, session.workspace.root)
    if problems:
        for text in problems:
            term.out(f"# {name}: {text}")
        return 1

    steps, notes = _plan(facts)
    outcome = run_batch(
        Batch(
            root=session.workspace.root,
            repos=(RepoPlan(repo=name, steps=steps, notes=notes),),
        ),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    if not outcome.mutated:
        return outcome.exit_code
    _drop_entry(session, name)
    return max(outcome.exit_code, refresh_after(session))


def _refusals(facts: RepoFacts, root: Path) -> list[str]:
    """Why the removal is refused: a shared rule, every dirty file, or lost detached commits."""
    reason = preflight.refusal(facts, "repo rm", root=root)
    if reason is not None:
        return [reason]
    lines = [f"modified {path}" for path in facts.modified]
    lines += [f"untracked {path}" for path in facts.untracked]
    if lines:
        lines.append("commit or stash these changes first")
    if facts.branch is None and facts.head is not None:
        orphans = orphan_commits(facts.bare, facts.head)
        if orphans:
            shown = git.show(("git", "-C", str(facts.path), "switch", "-c"), None)
            lines.append(
                f"detached HEAD has {orphans} commit(s) no remote has; "
                f"save them with `{shown} <branch>`"
            )
    return lines


def _plan(facts: RepoFacts) -> tuple[tuple[Step, ...], tuple[str, ...]]:
    """The worktree step, then the branch rule: a pushed branch goes, an unpushed one stays."""
    steps: tuple[Step, ...] = (
        GitStep(("git", "-C", str(facts.bare), "worktree", "remove", str(facts.path))),
    )
    if facts.branch is None:
        return steps, ()
    branch_steps, notes = branch_removal(facts.bare, facts.branch)
    return steps + tuple(branch_steps), notes


def _drop_missing(session: Session, name: str) -> int:
    """Drop a `[repos]` entry whose worktree is already gone."""
    if name not in session.accepted_bases:
        valid = ", ".join(entry.name for entry in session.locations) or "(none)"
        raise UsageError(f"unknown repo {name!r}; this workspace has: {valid}")
    term.out(f"# {name}: worktree already gone")
    _drop_entry(session, name)
    return refresh_after(session)


def _drop_entry(session: Session, name: str) -> None:
    """Rewrite `[repos]` without `name`; the reload of the refresh reads the new file."""
    assert session.config_path is not None  # a session with a workspace always has one
    merged = {repo: value for repo, value in session.accepted_bases.items() if repo != name}
    dropped = config.write_workspace(
        session.config_path,
        merged,
        dict(session.workspace_layer.values),
        declared=session.declared,
        remotes_of=remotes_of,
    )
    print_dropped(dropped)


def _url_for(session: Session, name: str, url: str | None, *, registered: bool) -> str:
    """The URL to register: the explicit one, else the repo's known one; a registered bare needs none."""
    if url is not None:
        return url
    known = known_repos(session.plugins)
    if name in known:
        return known[name]
    if registered:
        return ""
    raise UsageError(f"unknown repo {name}; pass a URL: ow repo add {name} <url>")


def _origin_url(bare: Path) -> str | None:
    """`origin`'s URL in `bare`, or None when it has none."""
    url = git.read(["git", "-C", str(bare), "remote", "get-url", "origin"], check=False)
    return url or None


def _record(session: Session, accepted: Mapping[str, str], name: str, resolved: str) -> int:
    """Rewrite `[repos]` with `name` at `resolved`, print dropped keys, then refresh."""
    merged = {**accepted, name: resolved}
    dropped = config.write_workspace(
        session.config_path,
        merged,
        dict(session.workspace_layer.values),
        declared=session.declared,
        remotes_of=remotes_of,
    )
    print_dropped(dropped)
    return refresh_workspace(session.reload())
