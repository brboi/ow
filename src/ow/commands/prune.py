import subprocess
from pathlib import Path
from typing import NamedTuple

from ow.utils.config import Config
from ow.utils.git import parallel_per_repo


class _PruneResult(NamedTuple):
    alias: str
    pruned_worktrees: bool
    deleted_branches: list[str]


def _prune_bare_repo(bare_repo: Path) -> _PruneResult:
    """Prune a single bare repo: clean worktrees and delete orphaned branches."""
    alias = bare_repo.stem
    pruned = False
    deleted: list[str] = []

    # 1. Worktree prune
    result = subprocess.run(
        ["git", "-C", str(bare_repo), "worktree", "prune"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        pruned = True

    # 2. Delete local branches not attached to any worktree
    wt_result = subprocess.run(
        ["git", "-C", str(bare_repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    used_branches: set[str] = set()
    if wt_result.returncode == 0:
        for line in wt_result.stdout.splitlines():
            if line.startswith("branch "):
                branch_ref = line.split(" ", 1)[1]
                if branch_ref.startswith("refs/heads/"):
                    used_branches.add(branch_ref[len("refs/heads/"):])

    branch_result = subprocess.run(
        ["git", "-C", str(bare_repo), "branch", "--list"],
        capture_output=True, text=True,
    )
    if branch_result.returncode == 0:
        all_branches = {b.strip().lstrip("*+ ") for b in branch_result.stdout.splitlines() if b.strip()}
        orphaned = all_branches - used_branches
        if orphaned:
            for branch in sorted(orphaned):
                subprocess.run(
                    ["git", "-C", str(bare_repo), "branch", "-D", branch],
                    capture_output=True, text=True,
                )
            deleted = sorted(orphaned)

    return _PruneResult(alias=alias, pruned_worktrees=pruned, deleted_branches=deleted)


def cmd_prune(config: Config) -> None:
    """Clean up stale worktree references and orphaned branches from bare repos."""
    bare_repos_dir = config.root_dir / ".bare-git-repos"
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
    for repo in bare_repos:
        alias = repo.stem
        result = prune_results.get(alias)
        if isinstance(result, Exception):
            continue
        if result.pruned_worktrees:
            print(f"  [{alias}] pruned stale worktrees")
            cleaned = True
        if result.deleted_branches:
            print(f"  [{alias}] deleted orphaned branches: {', '.join(result.deleted_branches)}")
            cleaned = True

    if not cleaned:
        print("All bare repos are clean.")
