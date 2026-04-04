from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ow.config import BranchSpec, RemoteConfig


def run_cmd(args: list[str], quiet: bool = False, label: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    if not quiet:
        if label:
            print(f"  [{label}] {' '.join(args)}", file=sys.stderr)
        else:
            print(f"    $ {' '.join(args)}", file=sys.stderr)
    return subprocess.run(args, **kwargs)


def ordered_remotes(alias_remotes: dict[str, RemoteConfig]) -> list[str]:
    result = []
    if "origin" in alias_remotes:
        result.append("origin")
    result.extend(sorted(r for r in alias_remotes if r != "origin"))
    return result


def ensure_bare_repo(
    alias: str,
    remotes: dict[str, RemoteConfig],
    bare_repos_dir: Path,
) -> None:
    bare_repo = bare_repos_dir / f"{alias}.git"
    if not bare_repo.exists():
        origin = remotes.get("origin")
        if not origin:
            raise ValueError(f"No origin remote configured for '{alias}'")
        run_cmd(
            [
                "git", "clone", "--bare", "--filter=blob:none",
                "--single-branch",
                origin.url, str(bare_repo),
            ],
            label=alias,
            check=True,
        )

    # Configure non-origin remotes (idempotent)
    for remote_name in ordered_remotes(remotes):
        remote_cfg = remotes[remote_name]
        if remote_name != "origin":
            run_cmd(
                ["git", "-C", str(bare_repo), "config", f"remote.{remote_name}.url", remote_cfg.url],
                quiet=True, check=True, label=alias,
            )
        if remote_cfg.pushurl:
            run_cmd(
                ["git", "-C", str(bare_repo), "config", f"remote.{remote_name}.pushurl", remote_cfg.pushurl],
                quiet=True, check=True, label=alias,
            )
        if remote_cfg.fetch:
            run_cmd(
                ["git", "-C", str(bare_repo), "config", f"remote.{remote_name}.fetch", remote_cfg.fetch],
                quiet=True, check=True, label=alias,
            )


def ensure_ref(bare_repo: Path, remote: str, branch: str) -> None:
    ref = f"refs/remotes/{remote}/{branch}"
    result = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
        capture_output=True,
    )
    if result.returncode != 0:
        run_cmd(
            ["git", "-C", str(bare_repo), "fetch", remote, f"{branch}:refs/remotes/{remote}/{branch}"],
            label=bare_repo.stem,
            check=True,
        )


def _ensure_base_ref_non_fatal(bare_repo: Path, spec: BranchSpec) -> None:
    """Ensure refs/remotes/spec.remote/spec.branch exists locally; non-fatal if it can't be fetched."""
    base_ref = f"refs/remotes/{spec.remote}/{spec.branch}"
    if subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", base_ref],
        capture_output=True,
    ).returncode != 0:
        subprocess.run(
            ["git", "-C", str(bare_repo), "fetch", spec.remote,
             f"{spec.branch}:refs/remotes/{spec.remote}/{spec.branch}"],
            capture_output=True,
        )


def resolve_spec(bare_repo: Path, spec: BranchSpec, alias_remotes: dict[str, RemoteConfig]) -> BranchSpec:
    """Find which remote actually has spec.branch; return updated BranchSpec with correct remote.

    If spec.local_branch already exists on a remote (i.e. already pushed), that remote
    branch is used as base_ref so the worktree tracks the correct upstream.
    """
    remotes_to_try = [spec.remote]
    for remote_name in ordered_remotes(alias_remotes):
        if remote_name not in remotes_to_try:
            remotes_to_try.append(remote_name)

    # First: if local_branch is set, check whether it already exists on a remote.
    # If it does, use that as base_ref so the worktree tracks its upstream.
    if spec.local_branch is not None:
        for remote in remotes_to_try:
            ref = f"refs/remotes/{remote}/{spec.local_branch}"
            result = subprocess.run(
                ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
                capture_output=True,
            )
            if result.returncode == 0:
                _ensure_base_ref_non_fatal(bare_repo, spec)
                return BranchSpec(f"{remote}/{spec.local_branch}", spec.local_branch)
            result = subprocess.run(
                ["git", "-C", str(bare_repo), "fetch", remote,
                 f"{spec.local_branch}:refs/remotes/{remote}/{spec.local_branch}"],
                capture_output=True,
            )
            if result.returncode == 0:
                _ensure_base_ref_non_fatal(bare_repo, spec)
                return BranchSpec(f"{remote}/{spec.local_branch}", spec.local_branch)

    # Fall through: find which remote has the base branch.
    for remote in remotes_to_try:
        ref = f"refs/remotes/{remote}/{spec.branch}"
        result = subprocess.run(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
            capture_output=True,
        )
        if result.returncode == 0:
            return BranchSpec(f"{remote}/{spec.branch}", spec.local_branch)
        result = subprocess.run(
            ["git", "-C", str(bare_repo), "fetch", remote,
             f"{spec.branch}:refs/remotes/{remote}/{spec.branch}"],
            capture_output=True,
        )
        if result.returncode == 0:
            return BranchSpec(f"{remote}/{spec.branch}", spec.local_branch)

    raise RuntimeError(f"Branch '{spec.branch}' not found on any configured remote")


def worktree_exists(bare_repo: Path, worktree_path: Path) -> bool:
    if not worktree_path.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(bare_repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    )
    return str(worktree_path) in result.stdout


def resolve_spec_local(bare_repo: Path, spec: BranchSpec, alias_remotes: dict[str, RemoteConfig]) -> BranchSpec:
    """Find which remote has spec.branch in local refs (no fetch). Raises RuntimeError if not found."""
    remotes_to_try = [spec.remote] + [r for r in ordered_remotes(alias_remotes) if r != spec.remote]
    for remote in remotes_to_try:
        ref = f"refs/remotes/{remote}/{spec.branch}"
        result = subprocess.run(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
            capture_output=True,
        )
        if result.returncode == 0:
            return BranchSpec(f"{remote}/{spec.branch}", spec.local_branch)
    raise RuntimeError(f"Branch '{spec.branch}' not found in local refs")


def _set_branch_upstream(bare_repo: Path, local_branch: str, remote: str, remote_branch: str) -> None:
    """Write branch.X.remote / branch.X.merge directly into the bare repo's git config.

    This is the correct mechanism for selective-fetch bare repos. The bare repo is cloned
    with --single-branch, so only the initial branch has a normal fetch refspec entry.
    Additional branches are fetched explicitly with custom mappings, intentionally outside
    the normal refspec — so `git branch --set-upstream-to` (which validates against the
    refspec before writing) would refuse. Writing branch.X.remote / branch.X.merge directly
    is the documented git mechanism that `--set-upstream-to` itself uses under the hood.
    """
    alias = bare_repo.stem
    run_cmd(
        ["git", "-C", str(bare_repo), "config", f"branch.{local_branch}.remote", remote],
        label=alias,
        check=True,
    )
    run_cmd(
        ["git", "-C", str(bare_repo), "config", f"branch.{local_branch}.merge", f"refs/heads/{remote_branch}"],
        label=alias,
        check=True,
    )


def create_worktree(bare_repo: Path, worktree_path: Path, spec: BranchSpec) -> None:
    alias = bare_repo.stem
    if spec.is_detached:
        run_cmd(
            ["git", "-C", str(bare_repo), "worktree", "add", "--detach", str(worktree_path), spec.base_ref],
            label=alias,
            check=True,
        )
    else:
        branch_exists = subprocess.run(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", f"refs/heads/{spec.local_branch}"],
            capture_output=True,
        ).returncode == 0
        if branch_exists:
            run_cmd(
                ["git", "-C", str(bare_repo), "worktree", "add", str(worktree_path), spec.local_branch],
                label=alias,
                check=True,
            )
        else:
            run_cmd(
                ["git", "-C", str(bare_repo), "worktree", "add", "-b", spec.local_branch, str(worktree_path), spec.base_ref],
                label=alias,
                check=True,
            )
        _set_branch_upstream(bare_repo, spec.local_branch, spec.remote, spec.branch)


def get_rev_list_count(repo_path: Path, ref_a: str, ref_b: str) -> tuple[int, int]:
    """Return (ahead, behind): ref_a ahead of ref_b, ref_a behind ref_b."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-list", "--left-right", "--count", f"{ref_a}...{ref_b}"],
        capture_output=True, text=True, check=True,
    )
    parts = result.stdout.strip().split()
    return int(parts[0]), int(parts[1])


def get_worktree_head(worktree_path: Path) -> tuple[str, str]:
    """Return (short_hash, full_hash)."""
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    full_hash = result.stdout.strip()
    return full_hash[:7], full_hash


def get_upstream(worktree_path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def worktree_is_detached(worktree_path: Path) -> bool:
    """True if HEAD is detached (no symbolic ref)."""
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
    )
    return result.returncode != 0


def get_worktree_branch(worktree_path: Path) -> str | None:
    """Return the current branch name, or None if HEAD is detached."""
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def attach_worktree(bare_repo: Path, worktree_path: Path, spec: BranchSpec) -> None:
    """Switch a detached worktree to a local branch tracking spec.base_ref."""
    alias = worktree_path.name
    branch_exists = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", f"refs/heads/{spec.local_branch}"],
        capture_output=True,
    ).returncode == 0
    if branch_exists:
        run_cmd(
            ["git", "-C", str(worktree_path), "switch", spec.local_branch],
            label=alias,
            check=True,
        )
    else:
        run_cmd(
            ["git", "-C", str(worktree_path), "switch", "-c", spec.local_branch],
            label=alias,
            check=True,
        )
    _set_branch_upstream(bare_repo, spec.local_branch, spec.remote, spec.branch)


def detach_worktree(worktree_path: Path, base_ref: str) -> None:
    """Switch an attached worktree to detached HEAD at base_ref."""
    run_cmd(
        ["git", "-C", str(worktree_path), "switch", "--detach", base_ref],
        label=worktree_path.name,
        check=True,
    )


def get_remote_ref_for_branch(
    repo: Path, local_branch: str, alias_remotes: dict,
    exclude_ref: str | None = None, base_remote: str | None = None,
) -> str | None:
    """Check all ow.toml-configured remotes for refs/remotes/{remote}/{local_branch}.

    Returns the first match (as "{remote}/{local_branch}"), excluding exclude_ref
    (typically spec.base_ref, to avoid returning the base branch itself).
    base_remote is checked last so fork remotes are preferred over the upstream.

    ``repo`` can be a bare repo or a worktree — any git repo path works.
    """
    remotes = ordered_remotes(alias_remotes)
    if base_remote and base_remote in remotes:
        remotes.remove(base_remote)
        remotes.append(base_remote)
    for remote in remotes:
        candidate = f"{remote}/{local_branch}"
        if candidate == exclude_ref:
            continue
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify",
             f"refs/remotes/{remote}/{local_branch}"],
            capture_output=True,
        )
        if result.returncode == 0:
            return candidate
    return None


def get_remote_url(bare_repo: Path, remote: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(bare_repo), "remote", "get-url", remote],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def git(repo: Path, *args, quiet: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Central git wrapper with automatic -C."""
    if repo.suffix == ".git" and repo.parent.name == ".bare-git-repos":
        label = repo.stem
    else:
        label = repo.name
    return run_cmd(["git", "-C", str(repo)] + list(args), quiet=quiet, label=label, **kwargs)


def git_fetch(repo: Path, remote: str, refspec: str, *, force: bool = False, **kwargs) -> None:
    """Fetch with optional force (+refspec)."""
    ref = f"+{refspec}" if force else refspec
    git(repo, "fetch", remote, ref, **kwargs)


def git_switch(worktree: Path, ref: str, *, detach: bool = False, create: bool = False, **kwargs) -> None:
    """Unified switch with detach/create options."""
    args = ["switch"]
    if detach:
        args.extend(["--detach", ref])
    elif create:
        args.extend(["-c", ref])
    else:
        args.append(ref)
    git(worktree, *args, **kwargs)


def git_rebase(worktree: Path, onto: str, **kwargs) -> subprocess.CompletedProcess:
    """Rebase onto ref. Returns CompletedProcess for caller to check."""
    return git(worktree, "rebase", onto, **kwargs)


def git_merge_base_fork_point(worktree: Path, upstream: str, branch: str) -> str | None:
    """Find fork-point between branch and upstream. None if upstream was rewritten."""
    result = git(
        worktree, "merge-base", "--fork-point", upstream, branch,
        quiet=True, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_rev_list(repo: Path, commit_range: str, *, reverse: bool = False) -> list[str]:
    """Return list of commit hashes in range. Empty list if range is invalid."""
    args = ["rev-list"]
    if reverse:
        args.append("--reverse")
    args.append(commit_range)
    result = git(repo, *args, quiet=True, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [h for h in result.stdout.strip().split("\n") if h]


def git_log_oneline(repo: Path, commit: str) -> str:
    """Return one-line log for a commit: 'hash message'."""
    result = git(repo, "log", "-1", "--format=%h %s", commit, quiet=True, capture_output=True, text=True)
    if result.returncode != 0:
        return commit[:7]
    return result.stdout.strip()


def git_cherry_pick(worktree: Path, commit: str) -> subprocess.CompletedProcess:
    """Cherry-pick a commit. Returns CompletedProcess for caller to check."""
    return git(worktree, "cherry-pick", commit)


def git_reset_hard(worktree: Path, ref: str) -> None:
    """Reset worktree to ref with --hard."""
    git(worktree, "reset", "--hard", ref, check=True)



