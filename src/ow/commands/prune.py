import sys
from pathlib import Path
from typing import NamedTuple

from ow.utils.display import err_console
from ow.utils import index, paths
from ow.utils.git import _run, parallel_per_repo


class _PrunePlan(NamedTuple):
    """What prune would do to one bare repo. Observation only, nothing applied.

    Deciding first and acting second is what makes --dry-run truthful and
    the confirmation worth answering: both show the very list that the
    apply step then works from, rather than a guess at it.
    """

    alias: str
    repo: Path
    stale_worktrees: list[str]
    to_delete: list[str]
    kept: list[str]

    @property
    def is_empty(self) -> bool:
        return not (self.stale_worktrees or self.to_delete or self.kept)

    @property
    def commands(self) -> list[list[str]]:
        argv: list[list[str]] = []
        if self.stale_worktrees:
            argv.append(["worktree", "prune"])
        argv.extend(["branch", "-D", branch] for branch in self.to_delete)
        return argv


class _PruneOutcome(NamedTuple):
    alias: str
    deleted: list[str]
    failed: list[tuple[str, str]]


def _survey_worktrees(bare_repo: Path) -> tuple[set[str], list[str]]:
    """One porcelain listing, read before pruning: (branches in use, stale paths).

    Before, not after: `git worktree prune` writes nothing to stdout without
    --verbose (and writes to stderr even with it), so asking it what it
    removed never got an answer. The listing marks a registered worktree
    whose directory has gone as `prunable`, which is the same judgement
    prune is about to act on — so read it while the evidence is still there.

    A prunable worktree's branch is deliberately not counted as in use: it
    is about to stop being attached to anything, and the branch pass has to
    see the repo as it will be, not as it was.
    """
    result = _run(
        ["git", "-C", str(bare_repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    used: set[str] = set()
    stale: list[str] = []
    if result.returncode != 0:
        return used, stale

    path: str | None = None
    branch: str | None = None
    prunable = False

    def close_block() -> None:
        nonlocal path, branch, prunable
        if path is not None:
            if prunable:
                stale.append(path)
            elif branch is not None:
                used.add(branch)
        path, branch, prunable = None, None, False

    for line in result.stdout.splitlines():
        if not line.strip():
            close_block()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            close_block()
            path = value
        elif key == "branch" and value.startswith("refs/heads/"):
            branch = value[len("refs/heads/"):]
        elif key == "prunable":
            prunable = True
    close_block()

    return used, stale


def _is_pushed(bare_repo: Path, branch: str) -> bool:
    """Is every commit on <branch> reachable from some refs/remotes/* ref?

    If it is, the branch is a label over history a remote already has, and
    deleting the label loses nothing. If it is not, the branch is the only
    thing keeping those commits alive: a bare repo has no reflog for them by
    default, so `git branch -D` leaves them unreachable, unnamed, and gone
    at the next gc.

    for-each-ref rather than `branch -r --contains`: it is plumbing, so no
    colour setting can turn its answer into escape codes. An unresolvable
    branch answers "not pushed" — refusing to delete is never the dangerous
    direction to be wrong in.
    """
    result = _run(
        [
            "git", "-C", str(bare_repo), "for-each-ref",
            "--contains", branch, "--format=%(refname)", "refs/remotes/",
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _survey_bare_repo(bare_repo: Path) -> _PrunePlan:
    """Work out what would go from one bare repo, without touching it."""
    used_branches, stale = _survey_worktrees(bare_repo)

    to_delete: list[str] = []
    kept: list[str] = []

    # for-each-ref, not `branch --list`: the latter is a porcelain command and
    # honours color.ui=always, which paints every name with escape codes that
    # no prefix-stripping removes. A branch name is an identifier we hand back
    # to git, not a string to display.
    branch_result = _run(
        ["git", "-C", str(bare_repo), "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True,
    )
    head_result = _run(
        ["git", "-C", str(bare_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    head_branch = head_result.stdout.strip() if head_result.returncode == 0 else None
    if head_branch == "HEAD":
        head_branch = None

    if branch_result.returncode == 0:
        all_branches = {b.strip() for b in branch_result.stdout.splitlines() if b.strip()}
        for branch in sorted(all_branches - used_branches):
            if branch == head_branch:
                # Never delete the branch HEAD points at — a dangling HEAD
                # confuses every subsequent git command and looks like data loss.
                continue
            # "Orphaned" describes the worktree that is gone, not the work
            # that may still be on the branch. Only the former is ours to
            # throw away.
            target = to_delete if _is_pushed(bare_repo, branch) else kept
            target.append(branch)

    return _PrunePlan(
        alias=bare_repo.stem, repo=bare_repo,
        stale_worktrees=stale, to_delete=to_delete, kept=kept,
    )


def _apply(plan: _PrunePlan) -> _PruneOutcome:
    """Run exactly what the plan showed, and report exactly what took."""
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []

    if plan.stale_worktrees:
        _run(
            ["git", "-C", str(plan.repo), "worktree", "prune"],
            capture_output=True, text=True,
        )

    for branch in plan.to_delete:
        # Only a delete git confirmed. Claiming a refusal as a deletion
        # sends the user looking elsewhere for work that is still here.
        result = _run(
            ["git", "-C", str(plan.repo), "branch", "-D", branch],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            deleted.append(branch)
        else:
            reason = result.stderr.strip().splitlines()[0] if result.stderr else "git refused"
            failed.append((branch, reason))

    return _PruneOutcome(alias=plan.alias, deleted=deleted, failed=failed)


def _dead_index_entries() -> int:
    """How many index lines name a workspace that is gone. Reads only.

    known_workspaces() prunes on read for two unrelated reasons: a line
    whose workspace no longer exists, and a duplicate of a line already
    seen. Only the former is a fact worth reporting — a duplicate is
    internal hygiene from a read-modify-write race in remember() (two
    concurrent writers), not something the user caused or can act on. So
    "dropped" here counts unique raw paths whose .ow/config.toml is gone,
    not the drop in line count, which would also count collapsed
    duplicates as deaths.

    This scan is done on the raw file, read before known_workspaces()
    rewrites it — reading it again afterwards would just see the
    already-pruned result.
    """
    index_file = paths.index_file()
    dropped = 0
    if index_file.exists():
        seen: set[Path] = set()
        for line in index_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            candidate = Path(line)
            if candidate in seen:
                continue
            seen.add(candidate)
            if not index._still_there(candidate):
                dropped += 1

    return dropped


def _index_line(dropped: int, verb: str) -> str:
    noun = "entry" if dropped == 1 else "entries"
    return f"{verb} {dropped} dead index {noun}."


def _confirm() -> bool:
    """Default is no. A destructive command must not proceed unasked."""
    try:
        answer = input("\nProceed? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _display_plan(plans: list[_PrunePlan]) -> None:
    """The whole report, in the imperative: nothing here has happened yet."""
    for plan in plans:
        if plan.stale_worktrees:
            noun = "worktree" if len(plan.stale_worktrees) == 1 else "worktrees"
            print(
                f"  [{plan.alias}] prune {len(plan.stale_worktrees)} stale {noun}: "
                f"{', '.join(plan.stale_worktrees)}"
            )
        if plan.to_delete:
            noun = "branch" if len(plan.to_delete) == 1 else "branches"
            print(
                f"  [{plan.alias}] delete {len(plan.to_delete)} orphaned {noun}: "
                f"{', '.join(plan.to_delete)}"
            )
        if plan.kept:
            noun = "branch" if len(plan.kept) == 1 else "branches"
            print(
                f"  [{plan.alias}] keep {len(plan.kept)} {noun} with unpushed commits: "
                f"{', '.join(plan.kept)}"
            )

    if any(plan.kept for plan in plans):
        print("\nKept branches hold commits no remote has. Push them, or delete one by hand:")
        for plan in plans:
            for branch in plan.kept:
                print(f"  git -C {plan.repo} branch -D {branch}")


def _display_dry_run(plans: list[_PrunePlan]) -> None:
    print("\nWould run:")
    for plan in plans:
        for argv in plan.commands:
            print(f"  [{plan.alias}] git {' '.join(argv)}")


def cmd_prune(*, dry_run: bool = False, yes: bool = False) -> None:
    """Clean up stale worktree references, orphaned branches, and dead index entries.

    Survey first, then act. --dry-run stops after the survey; otherwise the
    branch deletions — the only step that can lose work — are confirmed
    first, defaulting to no, exactly as `ow rebase` does. Answering no
    leaves everything untouched, the index included.
    """
    dropped = _dead_index_entries()

    bare_repos_dir = paths.repos_dir()
    bare_repos = sorted(bare_repos_dir.glob("*.git")) if bare_repos_dir.exists() else []
    if not bare_repos:
        print("No bare repos found.")

    surveyed = parallel_per_repo({
        repo.stem: (lambda r=repo: _survey_bare_repo(r))
        for repo in bare_repos
    })
    plans: list[_PrunePlan] = []
    survey_errors: dict[str, Exception] = {}
    for repo in bare_repos:
        result = surveyed.get(repo.stem)
        if isinstance(result, Exception):
            survey_errors[repo.stem] = result
        else:
            plans.append(result)

    for alias, exc in survey_errors.items():
        err_console.print(f"  [{alias}] survey failed: {exc}", markup=False)

    _display_plan(plans)
    if bare_repos and not survey_errors and all(plan.is_empty for plan in plans):
        print("All bare repos are clean.")

    if dry_run:
        if any(plan.commands for plan in plans):
            _display_dry_run(plans)
        if dropped:
            print(_index_line(dropped, "Would drop"))
        return

    if any(plan.to_delete for plan in plans) and not yes and not _confirm():
        print("Aborted.")
        return

    outcomes = parallel_per_repo({
        plan.alias: (lambda p=plan: _apply(p))
        for plan in plans
        if plan.commands
    })
    acted = False
    failed = bool(survey_errors)
    for plan in plans:
        outcome = outcomes.get(plan.alias)
        if isinstance(outcome, Exception):
            err_console.print(
                f"  [{plan.alias}] apply failed: {outcome}", markup=False,
            )
            failed = True
            continue
        if outcome is None:
            continue
        acted = acted or bool(outcome.deleted) or bool(plan.stale_worktrees)
        if outcome.failed:
            # git's refusals are the user's problem to act on, so they belong
            # on stderr where a pipeline can still see them.
            for branch, reason in outcome.failed:
                err_console.print(
                    f"  [{plan.alias}] could not delete {branch}: {reason}",
                    markup=False,
                )
            failed = True

    if dropped:
        index.known_workspaces()
        print(_index_line(dropped, "Dropped"))

    if acted:
        # The plan above is written in the imperative. Without this, silence
        # is all that separates "done" from "gave up somewhere".
        print("Done.")

    if failed:
        sys.exit(1)
