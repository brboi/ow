"""Plan execution: print steps, run them, report outcomes."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ow.core import facts, git
from ow.errors import OwError, UsageError
from ow import term
from ow.term import error, out, warn

_FORCED_UPDATE = re.compile(r"->\s*(\S+)\s+\(forced update\)")


@dataclass(frozen=True)
class GitStep:
    """A git command to run."""

    argv: tuple[str, ...]
    comment: str | None = None
    mutating: bool = True
    live: bool = False


@dataclass(frozen=True)
class OwStep:
    """A non-git action, e.g., removing a directory."""

    shown: str
    run: Callable[[], None]
    mutating: bool = True


Step = GitStep | OwStep


@dataclass(frozen=True)
class RepoPlan:
    """Plan for a single repository."""

    repo: str
    steps: tuple[Step, ...] = ()
    refusal: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Batch:
    """A batch of repository plans to execute."""

    root: Path | None
    repos: tuple[RepoPlan, ...]
    after: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Outcome:
    """Result of executing a batch."""

    ok: tuple[str, ...]
    failed: tuple[str, ...]
    refused: tuple[str, ...]
    mutated: bool
    exit_code: int
    after_failed: bool = False


def show(step: Step, root: Path | None = None) -> str:
    """Display a step for user consumption."""
    if isinstance(step, GitStep):
        return git.show(step.argv, root)
    return step.shown


def _print_steps(steps: Sequence[Step], root: Path | None = None) -> None:
    """Print each step, with its comment line when it has one."""
    for step in steps:
        if isinstance(step, GitStep) and step.comment:
            out(f"# {step.comment}")
        out(show(step, root))


def _err_lines(stderr: str, stdout: str = "") -> None:
    """Report git's stdout plainly, then its stderr as error lines."""
    for line in stdout.strip().splitlines():
        out(line)
    for line in stderr.strip().splitlines():
        error(line)


def _report_failure(repo: str | None, step_shown: str, stderr: str, stdout: str) -> None:
    """Print a failed step's command line, git output and auth hint."""
    if repo is None:
        error(step_shown)
    else:
        error(f"{repo}: {step_shown}")
    _err_lines(stderr, stdout)
    hint = git.auth_hint(stderr)
    if hint:
        warn(hint)


def _conflict_tail(step: GitStep, root: Path | None) -> bool:
    """Print labelled continue/abort lines; True when an operation is running."""
    if len(step.argv) < 3 or step.argv[1] != "-C":
        return False
    git_dir = Path(step.argv[2])
    try:
        operation = facts.operation_in_progress(git_dir)
    except (OSError, git.GitFailed):
        return False
    if operation is None or operation == "bisect":
        return False
    out("# resolve the conflict, then:")
    out(git.show(["git", "-C", str(git_dir), operation, "--continue"], root))
    out("# or give up:")
    out(git.show(["git", "-C", str(git_dir), operation, "--abort"], root))
    return True


def run_batch(batch: Batch, *, prompt: bool, dry_run: bool, yes: bool) -> Outcome:
    """Execute a batch of repository plans."""
    for repo_plan in batch.repos:
        if repo_plan.refusal:
            out(f"# {repo_plan.repo}: {repo_plan.refusal}")
        for note in repo_plan.notes:
            out(f"# {repo_plan.repo}: {note}")

    refused = tuple(rp.repo for rp in batch.repos if rp.refusal)
    plan_steps = [
        (repo_plan.repo, step)
        for repo_plan in batch.repos
        if not repo_plan.refusal
        for step in repo_plan.steps
    ]

    if prompt:
        _print_steps([step for _, step in plan_steps], batch.root)
        _print_steps(batch.after, batch.root)

        if not plan_steps and not batch.after:
            return Outcome((), (), refused, False, 1 if refused else 0)
        if dry_run:
            return Outcome((), (), refused, False, 1 if refused else 0)
        if not yes and not term.interactive():
            raise UsageError("no terminal to confirm; re-run with -y")
        if not yes and not term.confirm("Proceed?"):
            raise UsageError("aborted")
    elif dry_run:
        _print_steps([step for _, step in plan_steps], batch.root)
        _print_steps(batch.after, batch.root)
        return Outcome((), (), refused, False, 1 if refused else 0)

    ok: list[str] = []
    failed: list[str] = []
    mutated = False

    for repo_plan in batch.repos:
        if repo_plan.refusal:
            continue

        repo_ok = True
        for i, step in enumerate(repo_plan.steps):
            step_shown = show(step, batch.root)
            # Without a prompt there is no upfront plan; each line prints
            # just before it runs.
            if not prompt:
                _print_steps((step,), batch.root)

            if isinstance(step, GitStep):
                result = git.run(step.argv, live=step.live)
                if result.returncode != 0:
                    _report_failure(repo_plan.repo, step_shown, result.stderr, result.stdout)
                    remaining = repo_plan.steps[i + 1 :]
                    if _conflict_tail(step, batch.root) and remaining:
                        out("# then:")
                    if remaining:
                        _print_steps(remaining, batch.root)
                    repo_ok = False
                    break
                if step.mutating:
                    mutated = True
            else:
                try:
                    step.run()
                    if step.mutating:
                        mutated = True
                except (OSError, OwError) as e:
                    _report_failure(repo_plan.repo, step_shown, str(e))
                    repo_ok = False
                    break

        (ok if repo_ok else failed).append(repo_plan.repo)

    after_failed = False
    if failed:
        if batch.after:
            out("# not run:")
            _print_steps(batch.after, batch.root)
    else:
        for step in batch.after:
            step_shown = show(step, batch.root)
            if not prompt:
                _print_steps((step,), batch.root)

            if isinstance(step, GitStep):
                result = git.run(step.argv, live=step.live)
                if result.returncode != 0:
                    _report_failure(None, step_shown, result.stderr, result.stdout)
                    after_failed = True
                    break
                if step.mutating:
                    mutated = True
            else:
                try:
                    step.run()
                    if step.mutating:
                        mutated = True
                except (OSError, OwError) as e:
                    _report_failure(None, step_shown, str(e))
                    after_failed = True
                    break

    exit_code = 1 if (failed or refused or after_failed) else 0
    return Outcome(tuple(ok), tuple(failed), refused, mutated, exit_code, after_failed)


def run_fetches(
    steps_by_repo: Mapping[str, Sequence[GitStep]],
    root: Path | None = None,
    *,
    hint: str | None = "pass --no-fetch to work on cached refs",
) -> dict[str, git.GitFailed | None]:
    """Run fetch steps for multiple repositories in parallel.

    All fetch lines print first, then the repos are fetched in parallel and
    reported in the same order. Force-pushed refs and deleted upstreams are
    reported as notes; other failures print git's output and, once, the hint.
    """
    for repo_name in steps_by_repo:
        for step in steps_by_repo[repo_name]:
            out(show(step, root))

    repo_names = list(steps_by_repo.keys())
    with ThreadPoolExecutor() as executor:
        results = list(
            executor.map(lambda repo: _run_fetch_steps(repo, steps_by_repo[repo]), repo_names)
        )

    has_failures = False
    for repo, (failure, notes) in zip(repo_names, results):
        for note in notes:
            out(note)
        if failure is not None:
            has_failures = True
            _report_failure(repo, git.show(failure.argv, root), failure.stderr, failure.stdout)
    if has_failures and hint:
        warn(hint)

    return {repo: failure for repo, (failure, _) in zip(repo_names, results)}


def _run_fetch_steps(repo: str, steps: Sequence[GitStep]) -> tuple[git.GitFailed | None, tuple[str, ...]]:
    """Run one repo's fetch steps; returns (failure, notes)."""
    notes: list[str] = []
    for step in steps:
        result = git.run(step.argv, live=step.live)
        stderr = result.stderr or ""
        if result.returncode != 0:
            if (
                step.comment is not None
                and step.comment.startswith("upstream ")
                and "couldn't find remote ref" in stderr
            ):
                ref = step.comment[len("upstream ") :]
                notes.append(f"# {repo}: upstream {ref} is gone; `git branch --unset-upstream`")
                continue
            return git.GitFailed(step.argv, result.returncode, stderr, result.stdout or ""), tuple(notes)
        for line in stderr.splitlines():
            match = _FORCED_UPDATE.search(line)
            if match:
                notes.append(f"# {repo}: {match.group(1)} was force-pushed")
    return None, tuple(notes)
