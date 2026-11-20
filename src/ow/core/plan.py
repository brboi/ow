from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ow.core import git
from ow.errors import OwError, UsageError
from ow import term
from ow.term import error, out, warn

@dataclass(frozen=True)
class GitStep:
    """A git command to run."""

    argv: tuple[str, ...]
    comment: str | None = None
    mutating: bool = True


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


def show(step: Step, root: Path | None = None) -> str:
    """Display a step for user consumption.
    
    Args:
        step: GitStep or OwStep to display.
        root: Display base for worktree paths.
    
    Returns:
        String representation of the step.
    """
    if isinstance(step, GitStep):
        return git.show(step.argv, root)
    else:
        return step.shown


def _err_lines(text: str) -> None:
    """Report git's (possibly multi-line) stderr one prefixed line at a time."""
    for line in text.strip().splitlines():
        error(line)


def run_batch(
    batch: Batch, *, prompt: bool, dry_run: bool, yes: bool
) -> Outcome:
    """Execute a batch of repository plans.
    
    Args:
        batch: Batch to execute.
        prompt: Whether to prompt before execution.
        dry_run: Whether to perform a dry run.
        yes: Whether to automatically confirm.
    
    Returns:
        Outcome with results.
    """
    # Print refusals and notes first
    for repo_plan in batch.repos:
        if repo_plan.refusal:
            out(f"# {repo_plan.repo}: {repo_plan.refusal}")
        for note in repo_plan.notes:
            out(f"# {repo_plan.repo}: {note}")
    
    # Build list of all non-refusal steps with their comments
    plan_steps = []
    for repo_plan in batch.repos:
        if repo_plan.refusal:
            continue
        for step in repo_plan.steps:
            plan_steps.append((repo_plan.repo, step))
    
    # Check if there are any steps to execute
    has_executable_steps = bool(plan_steps) or bool(batch.after)
    
    if prompt:
        # Print the whole plan upfront
        for repo_name, step in plan_steps:
            if isinstance(step, GitStep) and step.comment:
                out(f"# {step.comment}")
            out(show(step, batch.root))
        
        # Print after steps
        for step in batch.after:
            if isinstance(step, GitStep) and step.comment:
                out(f"# {step.comment}")
            out(show(step, batch.root))
        
        # Handle prompting
        if not has_executable_steps:
            # No steps to run: return immediately
            refused = tuple(rp.repo for rp in batch.repos if rp.refusal)
            return Outcome(ok=(), failed=(), refused=refused, mutated=False, exit_code=1 if refused else 0)
        
        if dry_run:
            refused = tuple(rp.repo for rp in batch.repos if rp.refusal)
            return Outcome(ok=(), failed=(), refused=refused, mutated=False, exit_code=1 if refused else 0)
        
        if not yes and not term.interactive():
            raise UsageError("no terminal to confirm; re-run with -y")

        if not yes and not term.confirm("Proceed?"):
            raise UsageError("aborted")
    else:
        # prompt=False
        if dry_run:
            # Print the whole plan and return
            for repo_name, step in plan_steps:
                if isinstance(step, GitStep) and step.comment:
                    out(f"# {step.comment}")
                out(show(step, batch.root))
            
            # Print after steps
            for step in batch.after:
                if isinstance(step, GitStep) and step.comment:
                    out(f"# {step.comment}")
                out(show(step, batch.root))
            
            refused = tuple(rp.repo for rp in batch.repos if rp.refusal)
            return Outcome(ok=(), failed=(), refused=refused, mutated=False, exit_code=1 if refused else 0)
    
    # Execute steps
    ok = []
    failed = []
    mutated = False
    
    for repo_plan in batch.repos:
        if repo_plan.refusal:
            continue
        
        repo_ok = True
        for step in repo_plan.steps:
            step_shown = show(step, batch.root)
            # With a prompt the plan was printed whole before the prompt; an
            # additive command prints each line just before it runs (§13).
            if not prompt:
                if isinstance(step, GitStep) and step.comment:
                    out(f"# {step.comment}")
                out(step_shown)

            if isinstance(step, GitStep):
                # Run the git step
                result = git.run(step.argv)
                
                if result.returncode != 0:
                    error(f"{repo_plan.repo}: {step_shown}")
                    _err_lines(result.stderr)
                    # Detect operation in progress in the failed step's directory
                    git_dir = None
                    if len(step.argv) > 2 and step.argv[1] == "-C":
                        git_dir = Path(step.argv[2])
                    
                    if git_dir:
                        try:
                            # Get git paths for various operations
                            paths_result = git.run([
                                "git", "-C", str(git_dir), "rev-parse",
                                "--git-path", "rebase-merge",
                                "--git-path", "rebase-apply",
                                "--git-path", "CHERRY_PICK_HEAD",
                                "--git-path", "MERGE_HEAD",
                                "--git-path", "REVERT_HEAD",
                            ])
                            
                            if paths_result.returncode == 0:
                                git_paths = paths_result.stdout.rstrip("\n").split("\n")
                                
                                # Find which operation is in progress and print continuation/abort commands
                                for git_path_str in git_paths:
                                    git_path = Path(git_path_str)
                                    if not git_path.is_absolute():
                                        git_path = git_dir / git_path
                                    
                                    if git_path.exists():
                                        # Determine the operation type and print commands
                                        if "rebase-merge" in git_path_str or "rebase-apply" in git_path_str:
                                            out(git.show(["git", "-C", str(git_dir), "rebase", "--continue"], batch.root))
                                            out(git.show(["git", "-C", str(git_dir), "rebase", "--abort"], batch.root))
                                        elif "CHERRY_PICK_HEAD" in git_path_str:
                                            out(git.show(["git", "-C", str(git_dir), "cherry-pick", "--continue"], batch.root))
                                            out(git.show(["git", "-C", str(git_dir), "cherry-pick", "--abort"], batch.root))
                                        elif "MERGE_HEAD" in git_path_str:
                                            out(git.show(["git", "-C", str(git_dir), "merge", "--continue"], batch.root))
                                            out(git.show(["git", "-C", str(git_dir), "merge", "--abort"], batch.root))
                                        elif "REVERT_HEAD" in git_path_str:
                                            out(git.show(["git", "-C", str(git_dir), "revert", "--continue"], batch.root))
                                            out(git.show(["git", "-C", str(git_dir), "revert", "--abort"], batch.root))
                                        break  # Print only the first found operation
                        except (OSError, git.GitFailed):
                            pass  # Ignore detection failures
                    
                    # Print remaining steps of the repo
                    step_idx = repo_plan.steps.index(step)
                    for remaining_step in repo_plan.steps[step_idx + 1:]:
                        if isinstance(remaining_step, GitStep) and remaining_step.comment:
                            out(f"# {remaining_step.comment}")
                        out(show(remaining_step, batch.root))
                    
                    repo_ok = False
                    break
                
                if step.mutating:
                    mutated = True
            else:
                # OwStep
                try:
                    step.run()
                    if step.mutating:
                        mutated = True
                except (OSError, OwError) as e:
                    error(f"{repo_plan.repo}: {step_shown}")
                    _err_lines(str(e))
                    repo_ok = False
                    break
        
        if repo_ok:
            ok.append(repo_plan.repo)
        else:
            failed.append(repo_plan.repo)
    
    # Execute after steps (only if no repo failed)
    if not failed:
        for step in batch.after:
            step_shown = show(step, batch.root)
            if not prompt:
                if isinstance(step, GitStep) and step.comment:
                    out(f"# {step.comment}")
                out(step_shown)

            if isinstance(step, GitStep):
                # Run the git step
                result = git.run(step.argv)
                
                if result.returncode != 0:
                    error(step_shown)
                    _err_lines(result.stderr)
                    # Mark the after step as failed and stop processing
                    failed.append("<after>")
                    break
                
                if step.mutating:
                    mutated = True
            else:
                # OwStep
                try:
                    step.run()
                    if step.mutating:
                        mutated = True
                except (OSError, OwError) as e:
                    error(step_shown)
                    _err_lines(str(e))
                    # Mark the after step as failed and stop processing
                    failed.append("<after>")
                    break
    
    # Build outcome
    refused = tuple(rp.repo for rp in batch.repos if rp.refusal)
    ok_tuple = tuple(ok)
    failed_tuple = tuple(failed)
    exit_code = 1 if (failed_tuple or refused) else 0
    
    return Outcome(ok=ok_tuple, failed=failed_tuple, refused=refused, mutated=mutated, exit_code=exit_code)


def run_fetches(
    steps_by_repo: Mapping[str, Sequence[GitStep]], root: Path | None = None
) -> dict[str, git.GitFailed | None]:
    """Run fetch steps for multiple repositories in parallel.
    
    Args:
        steps_by_repo: Mapping of repo name to fetch steps.
        root: Display base for worktree paths.
    
    Returns:
        Dict mapping repo name to GitFailed exception or None if successful.
    """
    # Print all lines first
    for repo_name in steps_by_repo:
        for step in steps_by_repo[repo_name]:
            out(show(step, root))
    
    # Run fetches in parallel, maintaining order
    repo_names = list(steps_by_repo.keys())
    with ThreadPoolExecutor() as executor:
        # Use map to preserve order
        results_list = list(executor.map(
            lambda repo: _run_fetch_steps(repo, steps_by_repo[repo]),
            repo_names
        ))
    
    results = dict(zip(repo_names, results_list))
    
    # Report failures in order
    has_failures = False
    for repo in repo_names:
        if results[repo] is not None:
            has_failures = True
            git_failed = results[repo]
            error(f"{repo}: {git.show(git_failed.argv, root)}")
            _err_lines(git_failed.stderr)
    # Print hint once if there were failures
    if has_failures:
        warn("pass --no-fetch to work on cached refs")
    
    return results


def _run_fetch_steps(repo: str, steps: Sequence[GitStep]) -> git.GitFailed | None:
    """Run fetch steps for a single repo.
    
    Args:
        repo: Repository name.
        steps: Sequence of fetch steps to run.
    
    Returns:
        GitFailed exception if any step failed, None otherwise.
    """
    for step in steps:
        result = git.run(step.argv)
        if result.returncode != 0:
            return git.GitFailed(step.argv, result.returncode, result.stderr)
    
    return None
