from pathlib import Path
from typing import NamedTuple

from ow.utils.config import Config
from ow.utils import index, paths
from ow.utils.git import _run, parallel_per_repo


class _PruneResult(NamedTuple):
    alias: str
    stale_worktrees: list[str]
    deleted_branches: list[str]
    kept_branches: list[str]


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


def _prune_bare_repo(bare_repo: Path) -> _PruneResult:
    """Prune a single bare repo: clean worktrees and delete orphaned branches."""
    alias = bare_repo.stem
    deleted: list[str] = []
    kept: list[str] = []

    # 1. Look before pruning — afterwards there is nothing left to report on.
    used_branches, stale = _survey_worktrees(bare_repo)
    _run(
        ["git", "-C", str(bare_repo), "worktree", "prune"],
        capture_output=True, text=True,
    )

    # 2. Delete local branches not attached to any worktree
    # for-each-ref, not `branch --list`: the latter is a porcelain command and
    # honours color.ui=always, which paints every name with escape codes that
    # no prefix-stripping removes. A branch name is an identifier we hand back
    # to git, not a string to display.
    branch_result = _run(
        ["git", "-C", str(bare_repo), "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True,
    )
    if branch_result.returncode == 0:
        all_branches = {b.strip() for b in branch_result.stdout.splitlines() if b.strip()}
        for branch in sorted(all_branches - used_branches):
            # "Orphaned" describes the worktree that is gone, not the work
            # that may still be on the branch. Only the former is ours to
            # throw away.
            if not _is_pushed(bare_repo, branch):
                kept.append(branch)
                continue
            # Only a delete git confirmed. Claiming a refusal as a deletion
            # sends the user looking elsewhere for work that is still here.
            if _run(
                ["git", "-C", str(bare_repo), "branch", "-D", branch],
                capture_output=True, text=True,
            ).returncode == 0:
                deleted.append(branch)

    return _PruneResult(
        alias=alias, stale_worktrees=stale,
        deleted_branches=deleted, kept_branches=kept,
    )


def _prune_index() -> None:
    """Drop dead workspace-index entries and report how many disappeared.

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
            if not (candidate / index.MARKER).exists():
                dropped += 1

    index.known_workspaces()
    if dropped > 0:
        noun = "entry" if dropped == 1 else "entries"
        print(f"Dropped {dropped} dead index {noun}.")


def cmd_prune(config: Config) -> None:
    """Clean up stale worktree references, orphaned branches, and dead index entries."""
    _prune_index()

    bare_repos_dir = paths.repos_dir()
    if not bare_repos_dir.exists():
        print("No bare repos found.")
        return

    bare_repos = sorted(bare_repos_dir.glob("*.git"))
    if not bare_repos:
        print("No bare repos found.")
        return

    prune_tasks = {
        repo.stem: (lambda r=repo: _prune_bare_repo(r))
        for repo in bare_repos
    }
    prune_results = parallel_per_repo(prune_tasks)

    cleaned = False
    kept_any = False
    for repo in bare_repos:
        alias = repo.stem
        result = prune_results.get(alias)
        if isinstance(result, Exception):
            continue
        if result.stale_worktrees:
            noun = "worktree" if len(result.stale_worktrees) == 1 else "worktrees"
            print(
                f"  [{alias}] pruned {len(result.stale_worktrees)} stale {noun}: "
                f"{', '.join(result.stale_worktrees)}"
            )
            cleaned = True
        if result.deleted_branches:
            print(f"  [{alias}] deleted orphaned branches: {', '.join(result.deleted_branches)}")
            cleaned = True
        if result.kept_branches:
            noun = "branch" if len(result.kept_branches) == 1 else "branches"
            print(
                f"  [{alias}] kept {len(result.kept_branches)} {noun} with unpushed commits: "
                f"{', '.join(result.kept_branches)}"
            )
            cleaned = True
            kept_any = True

    if kept_any:
        print("\nKept branches hold commits no remote has. Push them, or delete one by hand:")
        print(f"  git -C {bare_repos_dir}/<alias>.git branch -D <branch>")

    if not cleaned:
        print("All bare repos are clean.")
