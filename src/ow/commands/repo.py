"""`ow repo add`, `ow repo ls` and `ow repo rm` (§6, §7, §4).

`add` registers a repo — creating its bare with the §6 recipe — and, inside a workspace, also
adds its worktree and records its default base. `ls` lists the bare repos ow knows, marking the
ones the session's workspace uses. `rm` removes a repo's worktree from this workspace.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ow import term
from ow.commands._shared import (
    accepted_repos,
    add_worktree,
    base_for,
    ensure_bare,
    print_dropped,
    remotes_of,
    refresh_after,
    refresh_workspace,
    run_steps,
)
from ow.core import config, git
from ow.core.paths import repos_dir
from ow.core.facts import gather_facts
from ow.core.plan import Batch, GitStep, RepoPlan, run_batch
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes import preflight
from ow.recipes.removal import branch_is_pushed


def add(session: Session, name: str, url: str | None, *, base: str | None) -> int:
    """Register `name` and, inside a workspace, add its worktree at its default base (§6).

    The URL defaults to the repo's known URL; an unknown repo without one is a usage error. An
    explicit URL wins for a repo that is not registered yet; an existing bare keeps its own
    origin, and the difference prints as a note. A re-run on a repo whose worktree is already
    present only records a new `--base`: no git command runs. Returns 1 when a git step failed.
    """
    url = _url_for(session, name, url)
    bare = repos_dir() / f"{name}.git"
    if bare.is_dir():
        registered = _origin_url(bare)
        if registered is not None and registered != url:
            term.out(
                f"# {name}: registered origin {registered} differs; keeping the registered one"
            )

    workspace = session.workspace
    if workspace is None:
        ensure_bare(session.plugins, name, url, remotes_of, None)
        return 0

    accepted = accepted_repos(session.config_path, session.declared)
    others = {repo: value for repo, value in accepted.items() if repo != name}
    if any(location.name == name for location in session.locations):
        if base is None:
            return 0
        resolved = base_for(session, name, base, accepted.get(name), others)
        return _record(session, accepted, name, resolved)

    ensure_bare(session.plugins, name, url, remotes_of, workspace.root)
    resolved = base_for(session, name, base, accepted.get(name), others)
    steps = add_worktree(
        bare, workspace.root / name, branch=None, base=resolved, remotes_of=remotes_of
    )
    code = run_steps(name, steps, root=workspace.root)
    if code:
        return code
    return _record(session, accepted, name, resolved)


def ls(session: Session) -> int:
    """One `<name>: <url>` line per bare repo, `(in this workspace)` on the ones used here (§4).

    A bare with no `origin` prints `(no origin)`. No bare repos prints nothing; the exit code is
    0 either way.
    """
    directory = repos_dir()
    if not directory.is_dir():
        return 0
    used = (
        {repo.name for repo in session.workspace.repos}
        if session.workspace is not None
        else set()
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
    """Remove `name`'s worktree from this workspace (§7): the plan, the prompt, the rewrite.

    The plan is `git -C <bare> worktree remove <path>` — never `--force`, git itself refuses
    modified or untracked files — followed by the branch rule of `rm`: a pushed branch goes
    with `branch -D`, an unpushed one stays and is listed as a kept note. The `[repos]` entry
    is dropped by a config rewrite after the batch, like the refresh that follows it; neither
    runs on a dry run, a refusal or a failure. Returns the batch's exit code, or the max of
    it and the refresh's.
    """
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")
    location = next((entry for entry in session.locations if entry.name == name), None)
    if location is None:
        valid = ", ".join(entry.name for entry in session.locations) or "(none)"
        raise UsageError(f"unknown repo {name!r}; this workspace has: {valid}")

    facts = gather_facts(location)
    refusal = preflight.refusal(facts, "repo rm")
    steps: list[GitStep] = []
    notes: list[str] = []
    if refusal is None:
        steps.append(
            GitStep(
                ("git", "-C", str(location.bare), "worktree", "remove", str(location.path))
            )
        )
        if facts.branch is not None:
            if branch_is_pushed(location.bare, facts.branch):
                steps.append(
                    GitStep(("git", "-C", str(location.bare), "branch", "-D", facts.branch))
                )
            else:
                notes.append(f"kept unpushed branch {facts.branch}")

    outcome = run_batch(
        Batch(
            root=session.workspace.root,
            repos=(RepoPlan(repo=name, steps=tuple(steps), refusal=refusal, notes=tuple(notes)),),
        ),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    if not outcome.mutated:
        return outcome.exit_code
    _drop_entry(session, name)
    return max(outcome.exit_code, refresh_after(session))


def _drop_entry(session: Session, name: str) -> None:
    """Rewrite `[repos]` without `name` (§7); the reload of the refresh reads the new file."""
    assert session.config_path is not None  # a session with a workspace always has one
    accepted = accepted_repos(session.config_path, session.declared)
    merged = {repo: value for repo, value in accepted.items() if repo != name}
    dropped = config.write_workspace(
        session.config_path,
        merged,
        dict(session.workspace_layer.values),
        declared=session.declared,
        remotes_of=remotes_of,
    )
    print_dropped(dropped)


def _url_for(session: Session, name: str, url: str | None) -> str:
    """The URL to register: the explicit one, else the repo's known one (§6)."""
    from ow.commands._shared import known_repos

    if url is not None:
        return url
    known = known_repos(session.plugins)
    if name in known:
        return known[name]
    raise UsageError(f"unknown repo {name}; pass a URL: ow repo add {name} <url>")


def _origin_url(bare: Path) -> str | None:
    """`origin`'s URL in `bare`, or None when it has none."""
    url = git.read(["git", "-C", str(bare), "remote", "get-url", "origin"], check=False)
    return url or None


def _record(session: Session, accepted: Mapping[str, str], name: str, resolved: str) -> int:
    """Rewrite `[repos]` with `name` at `resolved`, print dropped keys, then refresh (§6)."""
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
