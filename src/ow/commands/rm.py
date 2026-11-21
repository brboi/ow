"""`ow rm <ws|stack>`: remove a mono-stack workspace, a multi-stack workspace, or one stack."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ow import term
from ow.core import config, git, index, refresh, stacks
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
    """Remove a mono-stack workspace or a single stack; 1 when pre-flight refused."""
    workspace = session.workspace
    assert workspace is not None

    with term.json_mode(json_output):
        code, detail = _remove_one(session, dry_run=dry_run, yes=yes)
        if json_output:
            term.emit_json(envelope(detail))
        return code


def run_multi(
    plugins: Plugins,
    multi_root: Path,
    *,
    dry_run: bool,
    yes: bool,
    json_output: bool = False,
) -> int:
    """Remove every stack of multi-stack workspace `multi_root` in one batch and clean up."""
    loaded = stacks.load_multi_workspace(multi_root, plugins)
    if loaded is None:
        raise UsageError(f"no valid stacks found in {multi_root}")
    multi, sessions = loaded

    with term.json_mode(json_output):
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
                    "stack": ws.name,
                    "root": str(ws.root),
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
                        {
                            "workspace": multi.name,
                            "root": str(multi.root),
                            "multi_stack": True,
                            "stacks": preflight_refusals,
                            "exit_code": 1,
                        }
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

            def _drop_stack(root: Path = ws_root, stack_name: str = ws.name) -> None:
                shutil.rmtree(root)
                config.remove_stack(multi.root / ".ow" / "config.toml", stack_name)
                refresh.remove_lock_prefix(multi.root, stack_name)
                removed_roots.add(root)

            ws_steps.append(
                OwStep(shown=git.show(("rm", "-rf", str(ws_root))), run=_drop_stack)
            )
            plans.append(
                RepoPlan(
                    repo=f"stack:{ws.name}",
                    steps=tuple(ws_steps),
                    notes=tuple(ws_notes),
                )
            )

        def _finish_multi() -> None:
            remaining = stacks.load_multi_workspace(multi.root, plugins)
            if remaining is None:
                refresh.remove_owned_files(multi.root)
                cfg = multi.root / ".ow" / "config.toml"
                cfg.unlink(missing_ok=True)
                cfg.with_name(f".{cfg.name}.lock").unlink(missing_ok=True)
                try:
                    (multi.root / ".ow").rmdir()
                except OSError:
                    pass
                index.remove(multi.root)
                if multi.root.exists():
                    try:
                        if not any(multi.root.iterdir()):
                            multi.root.rmdir()
                    except OSError:
                        pass
            else:
                stacks.sync_multi_workspace_files(remaining[0], plugins)

        outcome = run_batch(
            Batch(
                root=None,
                repos=tuple(plans),
                after=(
                    OwStep(
                        shown=f"# clean up {git.display_path(multi.root)} and drop from index",
                        run=_finish_multi,
                    ),
                ),
            ),
            prompt=True,
            dry_run=dry_run,
            yes=yes,
        )

        if removed_roots and outcome.exit_code != 0 and not dry_run:
            _finish_multi()

        results = [
            {
                "stack": session.workspace.name,
                "root": str(session.workspace.root),
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
                    {
                        "workspace": multi.name,
                        "root": str(multi.root),
                        "multi_stack": True,
                        "stacks": results,
                        "exit_code": outcome.exit_code,
                    }
                )
            )
        return outcome.exit_code


def _remove_one(session: Session, *, dry_run: bool, yes: bool) -> tuple[int, dict[str, Any]]:
    workspace = session.workspace
    assert workspace is not None
    root = workspace.root
    is_stack = workspace.is_stack
    parent_multi = stacks.parent_multi_workspace(root) if is_stack else None
    facts = gather_all(session.locations)

    refusals = workspace_refusals(facts, root, session.locations)
    if refusals:
        for name, text in refusals:
            term.out(f"# {name}: {text}")
        return 1, {
            "workspace": workspace.name,
            "root": str(root),
            "is_stack": is_stack,
            "removed": False,
            "dry_run": dry_run,
            "refusals": [{"repo": n, "reason": t} for n, t in refusals],
        }

    outside = outside_entries(root, tuple(repo_facts.path for repo_facts in facts))
    if outside:
        term.out(f"# {outside}")

    def _finish_parent() -> None:
        if parent_multi is None:
            return
        config.remove_stack(parent_multi / ".ow" / "config.toml", workspace.name)
        refresh.remove_lock_prefix(parent_multi, workspace.name)
        remaining = stacks.load_multi_workspace(parent_multi, session.plugins)
        if remaining is None:
            refresh.remove_owned_files(parent_multi)
            index.remove(parent_multi)
        else:
            stacks.sync_multi_workspace_files(remaining[0], session.plugins)

    after_steps: list[Step] = [
        OwStep(shown=git.show(("rm", "-rf", str(root))), run=lambda: shutil.rmtree(root)),
    ]
    if is_stack and parent_multi is not None:
        after_steps.append(
            OwStep(
                shown=f"# update {parent_multi.name} workspace files",
                run=_finish_parent,
            )
        )
    else:
        after_steps.append(
            OwStep(
                shown=f"# drop {git.display_path(root)} from the index",
                run=lambda: index.remove(root),
            )
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
        "is_stack": is_stack,
        "removed": outcome.exit_code == 0 and not dry_run,
        "dry_run": dry_run,
        "refusals": [],
    }
