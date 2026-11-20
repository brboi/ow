"""`ow prune`: machine-wide cleanup of stale worktrees, orphan branches, dead index entries, and merged-PR workspaces."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ow import term
from ow.core import git, index, tasks
from ow.core.facts import gather_all
from ow.core.json_schema import envelope
from ow.core.paths import repos_dir
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, Step, run_batch
from ow.core.plugins import PruneCandidate
from ow.core.session import open_session
from ow.recipes.removal import branch_removal

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace


def run(
    *,
    plugins: "Plugins | None" = None,
    offline: bool = False,
    dry_run: bool,
    yes: bool,
    json_output: bool = False,
) -> int:
    """Survey every bare, the index, and plugin prune candidates, then run the machine-wide batch."""
    with term.json_mode(json_output):
        plans: list[RepoPlan] = []
        pruned_workspaces: list[dict[str, Any]] = []
        skipped_candidates: list[dict[str, Any]] = []
        after: list[Step] = []

        # 1. Plugin-recommended workspace removals (e.g. merged/closed GitHub PRs)
        if plugins is not None:
            live_workspaces = _load_live_workspaces(plugins)
            candidates = _collect_candidates(plugins, live_workspaces, offline=offline)
            from ow.commands.rm import _plan as rm_repo_plan, _refusals as rm_refusals

            for candidate in candidates:
                try:
                    sess = open_session(
                        str(candidate.workspace), need_workspace=True, plugins=plugins
                    )
                except Exception:
                    continue
                ws = sess.workspace
                if ws is None:
                    continue
                facts = gather_all(sess.locations)
                refusals = rm_refusals(facts, ws.root, sess.locations)
                if refusals:
                    reasons = "; ".join(f"{n}: {r}" for n, r in refusals)
                    term.out(
                        f"# skip pruning {ws.name} ({candidate.reason}): {reasons}"
                    )
                    skipped_candidates.append(
                        {
                            "workspace": ws.name,
                            "root": str(ws.root),
                            "reason": candidate.reason,
                            "refusals": [{"repo": n, "reason": r} for n, r in refusals],
                        }
                    )
                    continue

                term.out(f"# prune workspace {ws.name}: {candidate.reason}")
                for rf in facts:
                    plans.append(rm_repo_plan(rf))
                ws_root = ws.root
                ws_task = ws.task

                def _cleanup_ws(root: Path = ws_root, task_name: str | None = ws_task) -> None:
                    shutil.rmtree(root, ignore_errors=True)
                    index.remove(root)
                    if task_name and plugins is not None:
                        remaining = tasks.find_task(task_name, plugins)
                        if remaining is None:
                            tasks.cleanup_task_files(task_name)
                        else:
                            tasks.sync_task_files(remaining, plugins)

                after.append(
                    OwStep(
                        shown=git.show(("rm", "-rf", str(ws_root)))
                        + f"  # {candidate.reason}",
                        run=_cleanup_ws,
                    )
                )
                pruned_workspaces.append(
                    {"workspace": ws.name, "root": str(ws_root), "reason": candidate.reason}
                )

        # 2. Bare repo worktree prune + orphan pushed local branches
        for bare in _bares():
            plan, hints = _survey(bare)
            for line in hints:
                term.out(line)
            if plan is not None:
                plans.append(plan)

        # 3. Dead index entries
        dead = _dead_entries()
        for path in dead:
            after.append(
                OwStep(
                    shown=f"# drop {_display(path)} from the index",
                    run=lambda entry=path: index.remove(entry),
                )
            )

        outcome = run_batch(
            Batch(root=None, repos=tuple(plans), after=tuple(after)),
            prompt=True,
            dry_run=dry_run,
            yes=yes,
        )

        if json_output:
            term.emit_json(
                envelope(
                    {
                        "dry_run": dry_run,
                        "ok": list(outcome.ok),
                        "failed": list(outcome.failed),
                        "refused": list(outcome.refused),
                        "dead_index_entries": [str(p) for p in dead],
                        "pruned_workspaces": pruned_workspaces,
                        "skipped_workspaces": skipped_candidates,
                        "exit_code": outcome.exit_code,
                    }
                )
            )
        return outcome.exit_code


def _load_live_workspaces(plugins: "Plugins") -> tuple["Workspace", ...]:
    result: list[Workspace] = []
    for path in index.read():
        if not (path / ".ow" / "config.toml").exists():
            continue
        try:
            sess = open_session(str(path), need_workspace=False, plugins=plugins)
        except Exception:
            continue
        if sess.workspace is not None:
            result.append(sess.workspace)
    return tuple(result)


def _collect_candidates(
    plugins: "Plugins", workspaces: tuple["Workspace", ...], *, offline: bool
) -> list[PruneCandidate]:
    found: list[PruneCandidate] = []
    seen: set[Path] = set()
    for plugin_name, answer in plugins.call(
        "ow_prune_candidates",
        workspaces=workspaces,
        offline=offline,
        errors="collect",
    ):
        if isinstance(answer, Exception):
            term.warn(f"{plugin_name}: {answer}")
            continue
        if isinstance(answer, list):
            for item in answer:
                if isinstance(item, PruneCandidate) and item.workspace not in seen:
                    seen.add(item.workspace)
                    found.append(item)
    return found


def _bares() -> tuple[Path, ...]:
    """Every bare repo of `repos_dir()`, by name; a stray directory is not surveyed."""
    directory = repos_dir()
    if not directory.is_dir():
        return ()
    bares: list[Path] = []
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        if not entry.name.endswith(".git") or entry.name.startswith(".") or not entry.is_dir():
            continue
        result = git.run(["git", "-C", str(entry), "rev-parse", "--is-bare-repository"])
        if result.returncode == 0 and result.stdout.strip() == "true":
            bares.append(entry)
    return tuple(bares)


def _survey(bare: Path) -> tuple[RepoPlan | None, tuple[str, ...]]:
    """What `bare` needs, plus one hint line per prunable worktree."""
    prunable, checked_out = _worktrees(bare)
    hints = tuple(
        f"# {_display(path)}: prunable (directory gone)"
        for path in prunable
    )
    steps: list[Step] = []
    if prunable:
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "prune")))
    notes: list[str] = []
    head_branch = _head_branch(bare)
    for branch in _local_branches(bare):
        if branch in checked_out or branch == head_branch:
            continue
        branch_steps, branch_notes = branch_removal(bare, branch)
        steps.extend(branch_steps)
        notes.extend(branch_notes)
    if not steps and not notes:
        return None, hints
    return RepoPlan(repo=bare.stem, steps=tuple(steps), notes=tuple(notes)), hints


def _head_branch(bare: Path) -> str | None:
    result = git.run(["git", "-C", str(bare), "symbolic-ref", "--quiet", "--short", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else None


def _worktrees(bare: Path) -> tuple[tuple[Path, ...], frozenset[str]]:
    listing = git.read(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
    prunable: list[Path] = []
    checked_out: set[str] = set()
    path: Path | None = None
    branch: str | None = None
    is_prunable = False

    def close() -> None:
        nonlocal path, branch, is_prunable
        if path is not None:
            if is_prunable:
                prunable.append(path)
            if branch is not None:
                checked_out.add(branch)
        path, branch, is_prunable = None, None, False

    for line in listing.split("\n"):
        if not line:
            close()
        elif line.startswith("worktree "):
            close()
            path = Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif line == "prunable" or line.startswith("prunable "):
            is_prunable = True
    close()
    return tuple(prunable), frozenset(checked_out)


def _local_branches(bare: Path) -> tuple[str, ...]:
    output = git.read(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads/"]
    )
    return tuple(line.strip() for line in output.split("\n") if line.strip())


def _dead_entries() -> tuple[Path, ...]:
    return tuple(path for path in index.read() if not (path / ".ow" / "config.toml").exists())


def _display(path: Path) -> str:
    return git.show(("git", "-C", str(path))).removeprefix("git -C ")
