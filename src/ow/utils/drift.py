import sys
from dataclasses import dataclass

from ow.utils.config import BranchSpec, WorkspaceConfig
from ow.utils.git import get_worktree_branch, parallel_per_repo


@dataclass
class DriftResult:
    """Result of checking worktree drift from config."""
    alias: str
    spec: BranchSpec
    actual_branch: str | None

    @property
    def is_drifted(self) -> bool:
        if self.spec.is_detached:
            return self.actual_branch is not None
        return self.actual_branch != self.spec.local_branch

    @property
    def message(self) -> str:
        if self.spec.is_detached:
            expected = f"detached at {self.spec.base_ref}"
        else:
            expected = f"branch {self.spec.local_branch}"
        if self.actual_branch is None:
            actual = "detached HEAD"
        else:
            actual = f"branch {self.actual_branch}"
        return f"{self.alias}: expected {expected}, found {actual}"


def check_drift(worktree_path, spec: BranchSpec, alias: str) -> DriftResult:
    """Check if worktree state matches config spec."""
    actual_branch = get_worktree_branch(worktree_path)
    return DriftResult(alias=alias, spec=spec, actual_branch=actual_branch)


def check_all_drift(ws: WorkspaceConfig, ws_dir) -> list[DriftResult]:
    """Every drifted repo, in config order. No output."""
    from typing import Any

    drift_tasks: dict[str, Any] = {}
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            continue
        drift_tasks[alias] = (lambda w=worktree_path, s=spec, a=alias: check_drift(w, s, a))

    if not drift_tasks:
        return []

    drift_results = parallel_per_repo(drift_tasks)

    drifted: list[DriftResult] = []
    for alias in ws.repos:
        result = drift_results.get(alias)
        if result and not isinstance(result, Exception) and result.is_drifted:
            drifted.append(result)
    return drifted


def print_drift_warning(drifted: list[DriftResult]) -> None:
    """The stderr warning block — unchanged text."""
    if not drifted:
        return
    print("Warning: drift detected between config and worktree state:", file=sys.stderr)
    for d in drifted:
        print(f"  {d.message}", file=sys.stderr)
    print("  Run `ow apply` to realign the worktree(s) with the config.", file=sys.stderr)


def warn_if_drifted(ws: WorkspaceConfig, ws_dir) -> bool:
    """Display warnings for drift; return True if any drifted. Never exits."""
    drifted = check_all_drift(ws, ws_dir)
    print_drift_warning(drifted)
    return bool(drifted)
