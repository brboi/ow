"""`ow rm <ws>` / `ow rm -t <task>`: remove workspace(s), their worktrees, pushed branches and task files."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ow import term
from ow.core import git, index, tasks
from ow.core.facts import RepoFacts, gather_all
from ow.core.json_schema import envelope
from ow.core.plan import Batch, OwStep, RepoPlan, Step, run_batch
from ow.core.plugins import Plugins
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes.removal import outside_entries, repo_removal_plan, workspace_refusals


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
    """Remove every workspace belonging to `task_name` in one batch and clean up task files."""
    loaded = tasks.load_task_sessions(task_name, plugins)
    if loaded is None:
        raise UsageError(f"unknown task {task_name!r}")
    _task, sessions = loaded

    with term.json_mode(json_output):
        # Preflight every workspace once before building a single batch and prompting once.
        gathered: list[tuple[Session, tuple[RepoFacts, ...]]] = []
        preflight_refusals: list[dict[str, Any]] = []
        any_refused = False
        for session in sessions:
            assert session.workspace is not None
            ws = session.workspace
            for warning in session.warnings:
                term.warn(warning)
            facts = gather_all(session.locations)
            refusals = workspace_refusals(facts, ws.root, session.locations)
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
            gathered.append((session, facts))

        if any_refused:
            if json_output:
                term.emit_json(
                    envelope(
                        {"task": task_name, "workspaces": preflight_refusals, "exit_code": 1}
                    )
                )
            return 1

        plans: list[RepoPlan] = []
        removed_roots: set[Path] = set()
        for session, facts in gathered:
            assert session.workspace is not None
            ws = session.workspace
            outside = outside_entries(ws.root, tuple(rf.path for rf in facts))
            if outside:
                term.out(f"# {ws.name}: {outside}")
            ws_steps: list[Step] = []
            ws_notes: list[str] = []
            for rf in facts:
                rp = repo_removal_plan(rf)
                ws_steps.extend(rp.steps)
                ws_notes.extend(rp.notes)
            ws_root = ws.root

            def _drop_ws(root: Path = ws_root) -> None:
                shutil.rmtree(root)
                index.remove(root)
                removed_roots.add(root)

            ws_steps.append(
                OwStep(shown=git.show(("rm", "-rf", str(ws_root))), run=_drop_ws)
            )
            plans.append(
                RepoPlan(
                    repo=f"workspace:{ws.name}",
                    steps=tuple(ws_steps),
                    notes=tuple(ws_notes),
                )
            )

        def _finish_task() -> None:
            remaining = tasks.find_task(task_name, plugins)
            if remaining is None:
                tasks.cleanup_task_files(task_name)
            else:
                tasks.sync_task_files(remaining, plugins)

        outcome = run_batch(
            Batch(
                root=None,
                repos=tuple(plans),
                after=(
                    OwStep(shown=f"# update task {task_name!r} files", run=_finish_task),
                ),
            ),
            prompt=True,
            dry_run=dry_run,
            yes=yes,
        )

        # If some workspaces were deleted before a failure prevented `after`, still sync task files.
        if removed_roots and outcome.exit_code != 0 and not dry_run:
            _finish_task()

        results = [
            {
                "workspace": session.workspace.name,
                "root": str(session.workspace.root),
                "task": task_name,
                "removed": session.workspace.root in removed_roots and not dry_run,
                "dry_run": dry_run,
                "refusals": [],
            }
            for session, _facts in gathered
            if session.workspace is not None
        ]
        if json_output:
            term.emit_json(
                envelope(
                    {"task": task_name, "workspaces": results, "exit_code": outcome.exit_code}
                )
            )
        return outcome.exit_code


def _remove_one(session: Session, *, dry_run: bool, yes: bool) -> tuple[int, dict[str, Any]]:
    workspace = session.workspace
    assert workspace is not None
    root = workspace.root
    task_name = workspace.task
    facts = gather_all(session.locations)

    refusals = workspace_refusals(facts, root, session.locations)
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

    outside = outside_entries(root, tuple(repo_facts.path for repo_facts in facts))
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
        OwStep(
            shown=f"# drop {git.display_path(root)} from the index",
            run=lambda: index.remove(root),
        ),
    ]
    if task_name:
        after_steps.append(
            OwStep(shown=f"# update task {task_name!r} files", run=_finish_task)
        )

    outcome = run_batch(
        Batch(
            root=root,
            repos=tuple(repo_removal_plan(repo_facts) for repo_facts in facts),
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
