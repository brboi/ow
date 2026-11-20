"""`ow rm <ws>` / `ow rm -t <task>`: remove workspace(s), their worktrees, pushed branches and task files."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Sequence

from ow import term
from ow.commands.prune import _display
from ow.core import git, index, tasks
from ow.core.facts import RepoFacts, gather_all
from ow.core.json_schema import envelope
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, Step, run_batch
from ow.core.plugins import Plugins
from ow.core.session import Session, open_session
from ow.core.workspace import RepoLocation, stray_checkouts
from ow.errors import UsageError
from ow.recipes import preflight
from ow.recipes.removal import branch_removal, orphan_commits

DIRTY = "commit, stash or discard, then re-run"


def run(
    session: Session,
    *,
    dry_run: bool,
    yes: bool,
    json_output: bool = False,
) -> int:
    """Remove the session's workspace; 1 when pre-flight refused, else the batch's code."""
    workspace = session.workspace
    assert workspace is not None

    with term.json_mode(json_output):
        code, detail = _remove_one(session, dry_run=dry_run, yes=yes)
        if json_output:
            term.emit_json(envelope(detail))
        return code


def run_task(
    plugins: Plugins,
    task_name: str,
    *,
    dry_run: bool,
    yes: bool,
    json_output: bool = False,
) -> int:
    """Remove every workspace belonging to `task_name` and clean up task-level files."""
    task = tasks.find_task(task_name, plugins)
    if task is None:
        raise UsageError(f"unknown task {task_name!r}")

    with term.json_mode(json_output):
        # Preflight every workspace in the task before mutating any of them.
        sessions: list[Session] = []
        preflight_refusals: list[dict[str, Any]] = []
        any_refused = False
        for ws in task.workspaces:
            session = open_session(str(ws.root), need_workspace=True, plugins=plugins)
            for warning in session.warnings:
                term.warn(warning)
            sessions.append(session)
            facts = gather_all(session.locations)
            refusals = _refusals(facts, ws.root, session.locations)
            if refusals:
                any_refused = True
                for name, text in refusals:
                    term.out(f"# {ws.name}/{name}: {text}")
            preflight_refusals.append(
                {
                    "workspace": ws.name,
                    "root": str(ws.root),
                    "task": task_name,
                    "removed": False,
                    "dry_run": dry_run,
                    "refusals": [{"repo": n, "reason": t} for n, t in refusals],
                }
            )

        if any_refused:
            if json_output:
                term.emit_json(
                    envelope(
                        {"task": task_name, "workspaces": preflight_refusals, "exit_code": 1}
                    )
                )
            return 1

        results: list[dict[str, Any]] = []
        exit_code = 0
        for session in sessions:
            code, detail = _remove_one(session, dry_run=dry_run, yes=yes)
            results.append(detail)
            exit_code = max(exit_code, code)

        if json_output:
            term.emit_json(
                envelope({"task": task_name, "workspaces": results, "exit_code": exit_code})
            )
        return exit_code


def _remove_one(session: Session, *, dry_run: bool, yes: bool) -> tuple[int, dict[str, Any]]:
    workspace = session.workspace
    assert workspace is not None
    root = workspace.root
    task_name = workspace.task
    facts = gather_all(session.locations)

    refusals = _refusals(facts, root, session.locations)
    if refusals:
        for name, text in refusals:
            term.out(f"# {name}: {text}")
        return 1, {
            "workspace": workspace.name,
            "root": str(root),
            "task": task_name,
            "removed": False,
            "dry_run": dry_run,
            "refusals": [{"repo": n, "reason": t} for n, t in refusals],
        }

    outside = _outside(root, tuple(repo_facts.path for repo_facts in facts))
    if outside:
        term.out(f"# {outside}")

    def _finish_task() -> None:
        if not task_name:
            return
        remaining = tasks.find_task(task_name, session.plugins)
        if remaining is None:
            tasks.cleanup_task_files(task_name)
        else:
            tasks.sync_task_files(remaining, session.plugins)

    after_steps: list[Step] = [
        OwStep(shown=git.show(("rm", "-rf", str(root))), run=lambda: shutil.rmtree(root)),
        OwStep(shown=f"# drop {_display(root)} from the index", run=lambda: index.remove(root)),
    ]
    if task_name:
        after_steps.append(
            OwStep(shown=f"# update task {task_name!r} files", run=_finish_task)
        )

    outcome = run_batch(
        Batch(
            root=root,
            repos=tuple(_plan(repo_facts) for repo_facts in facts),
            after=tuple(after_steps),
        ),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    return outcome.exit_code, {
        "workspace": workspace.name,
        "root": str(root),
        "task": task_name,
        "removed": outcome.exit_code == 0 and not dry_run,
        "dry_run": dry_run,
        "refusals": [],
    }


def _refusals(
    facts: Sequence[RepoFacts], root: Path, locations: Sequence[RepoLocation]
) -> tuple[tuple[str, str], ...]:
    """Why the whole removal is refused: dirty files, unsafe HEADs, unmanaged checkouts."""
    lines: list[tuple[str, str]] = []
    for repo_facts in facts:
        reason = preflight.refusal(repo_facts, "rm", root=root)
        if reason is not None:
            lines.append((repo_facts.name, reason))
            continue
        for path in repo_facts.modified:
            lines.append((repo_facts.name, f"modified {path}; {DIRTY}"))
        for path in repo_facts.untracked:
            lines.append((repo_facts.name, f"untracked {path}; {DIRTY}"))
        if repo_facts.branch is None and repo_facts.head is not None:
            orphans = orphan_commits(repo_facts.bare, repo_facts.head)
            if orphans:
                lines.append(
                    (
                        repo_facts.name,
                        f"{orphans} commit(s) on no branch; keep them with "
                        f"`git -C {_display(repo_facts.path)} switch -c <b>`, or discard them,"
                        " then re-run",
                    )
                )
    for stray in stray_checkouts(root, locations):
        lines.append((stray.name, "a checkout ow does not manage; move it away or delete it, then re-run"))
    return tuple(lines)


def _plan(repo_facts: RepoFacts) -> RepoPlan:
    """One repo: the worktree goes, and its branch goes only when pushed."""
    steps: list[Step] = [
        GitStep(("git", "-C", str(repo_facts.bare), "worktree", "remove", str(repo_facts.path)))
    ]
    notes: tuple[str, ...] = ()
    if repo_facts.branch is not None:
        branch_steps, notes = branch_removal(repo_facts.bare, repo_facts.branch)
        steps.extend(branch_steps)
    return RepoPlan(repo=repo_facts.name, steps=tuple(steps), notes=notes)


def _outside(root: Path, repos: Sequence[Path]) -> str:
    """The top-level entries `rm -rf` takes besides the repo worktrees, with file counts."""
    entries: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if any(entry.is_relative_to(repo) for repo in repos):
            continue
        if not entry.is_dir():
            entries.append(entry.name)
            continue
        count = sum(1 for child in entry.rglob("*") if child.is_file())
        plural = "s" if count != 1 else ""
        entries.append(f"{entry.name}/ ({count} file{plural})" if count else f"{entry.name}/")
    return ", ".join(entries)
