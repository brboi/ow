import re
from pathlib import Path
from typing import Any, NamedTuple

from ow.utils.display import c, counts, osc8
from ow.utils.drift import warn_if_drifted
from ow.utils.refs import fetch_workspace_refs
from ow.utils.resolver import resolve_workspace
from ow.utils.config import BranchSpec, Config
from ow.utils.git import (
    get_all_remote_refs,
    get_remote_ref_for_branch,
    get_remote_url,
    get_rev_list_count,
    get_upstream,
    get_worktree_head,
    parallel_per_repo,
)

# ---------------------------------------------------------------------------
# Display helpers for status
# ---------------------------------------------------------------------------


def _github_url_from_remote(remote_url: str) -> str | None:
    """Parse git remote URL to GitHub web URL."""
    ssh_match = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if ssh_match:
        return f"https://github.com/{ssh_match.group(1)}/{ssh_match.group(2)}"
    https_match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if https_match:
        return f"https://github.com/{https_match.group(1)}/{https_match.group(2)}"
    return None


class _StatusResult(NamedTuple):
    status_line: str
    first_attached_branch: str | None
    github_link: tuple[str, str] | None


def _display_detached_status(
    alias: str,
    spec: BranchSpec,
    resolved: BranchSpec,
    worktree_path: Path,
    max_alias_len: int,
) -> str:
    """Format status line for a detached worktree."""
    padding = " " * (max_alias_len - len(alias) + 1)
    ahead, behind = get_rev_list_count(worktree_path, "HEAD", resolved.base_ref)
    short_hash, _ = get_worktree_head(worktree_path)

    status = f"{c(resolved.base_ref, 1)} {counts(behind, ahead)} ({c('DETACHED', 33)}: {short_hash})"
    return f"        {alias}:{padding}{status}"


def _display_attached_status(
    alias: str,
    spec: BranchSpec,
    resolved: BranchSpec,
    worktree_path: Path,
    max_alias_len: int,
    *,
    refs: set[str] | None = None,
) -> str:
    """Format status line for an attached worktree."""
    padding = " " * (max_alias_len - len(alias) + 1)

    remote_ref = get_remote_ref_for_branch(
        worktree_path,
        resolved.local_branch,
        {},
        exclude_ref=resolved.base_ref,
        base_remote=resolved.remote,
        refs=refs,
    )
    if remote_ref:
        ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", remote_ref)
        ahead_base, behind_base = get_rev_list_count(worktree_path, remote_ref, resolved.base_ref)
        status = f"{c(remote_ref, 1)} {counts(behind_up, ahead_up)} ({c(resolved.base_ref, 1)} {counts(behind_base, ahead_base)})"
    else:
        upstream = get_upstream(worktree_path)
        if upstream:
            ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", upstream)
            if upstream != resolved.base_ref:
                ahead_base, behind_base = get_rev_list_count(worktree_path, upstream, resolved.base_ref)
                status = f"{c(upstream, 1)} {counts(behind_up, ahead_up)} ({c(resolved.base_ref, 1)} {counts(behind_base, ahead_base)})"
            else:
                status = f"{c(resolved.local_branch, 1)} {c('(local)', 2)} ({c(upstream, 1)} {counts(behind_up, ahead_up)})"
        else:
            ahead_base, behind_base = get_rev_list_count(worktree_path, "HEAD", resolved.base_ref)
            status = f"{c(resolved.local_branch, 1)} {c('(local)', 2)} ({c(resolved.base_ref, 1)} {counts(behind_base, ahead_base)})"

    return f"        {alias}:{padding}{status}"


def _gather_repo_status(
    alias: str, spec: BranchSpec, resolved: BranchSpec,
    worktree_path: Path, bare_repo: Path, max_alias_len: int,
    refs: set[str],
) -> _StatusResult:
    """Gather all display data for one repo (runs in parallel)."""
    if resolved.is_detached:
        status_line = _display_detached_status(alias, spec, resolved, worktree_path, max_alias_len)
        short_hash, _ = get_worktree_head(worktree_path)
        remote_url = get_remote_url(bare_repo, resolved.remote)
        link = None
        if remote_url:
            github_base = _github_url_from_remote(remote_url)
            if github_base:
                link = (alias, f"{github_base}/commit/{short_hash}")
        return _StatusResult(status_line, None, link)
    else:
        status_line = _display_attached_status(
            alias, spec, resolved, worktree_path, max_alias_len, refs=refs,
        )
        remote_url = get_remote_url(bare_repo, resolved.remote)
        link = None
        if remote_url:
            github_base = _github_url_from_remote(remote_url)
            if github_base:
                link = (alias, f"{github_base}/tree/{resolved.local_branch}")
        return _StatusResult(status_line, resolved.local_branch, link)


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------


def cmd_status(config: Config, workspace: str | None = None) -> None:
    """Show branch status for the current workspace."""
    ws_dir, ws = resolve_workspace(config, name=workspace)
    bare_repos_dir = config.root_dir / ".bare-git-repos"

    warn_if_drifted(ws, ws_dir)

    _, _, resolved_specs = fetch_workspace_refs(ws, ws_dir, config, fetch_upstreams=True)

    print(c(f"[{ws_dir.name}]", 1, 36))
    print("    " + c("branches", 2))

    max_alias_len = max((len(a) for a in ws.repos), default=0)

    # Build parallel tasks for repos that exist and have resolved specs
    status_tasks: dict[str, Any] = {}
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            continue
        resolved = resolved_specs.get(alias)
        if resolved is None:
            continue
        bare_repo = bare_repos_dir / f"{alias}.git"
        refs = get_all_remote_refs(bare_repo)
        status_tasks[alias] = (
            lambda a=alias, s=spec, r=resolved, w=worktree_path, b=bare_repo, rf=refs:
            _gather_repo_status(a, s, r, w, b, max_alias_len, rf)
        )

    if status_tasks:
        status_results = parallel_per_repo(status_tasks)
    else:
        status_results = {}

    # Print results in config order
    first_attached_branch: str | None = None
    github_links: list[tuple[str, str]] = []

    for alias, spec in ws.repos.items():
        padding = " " * (max_alias_len - len(alias) + 1)
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            print(f"        {alias}:{padding}{c('(not applied)', 2)}")
            continue

        resolved = resolved_specs.get(alias)
        if resolved is None:
            print(f"        {alias}:{padding}{c('(error: could not resolve)', 31)}")
            continue

        result = status_results.get(alias)
        if isinstance(result, Exception):
            print(f"        {alias}:{padding}{c('(error)', 31)}")
            continue

        print(result.status_line)
        if first_attached_branch is None and result.first_attached_branch:
            first_attached_branch = result.first_attached_branch
        if result.github_link:
            github_links.append(result.github_link)

    print("    " + c("links", 2))
    if first_attached_branch:
        runbot_url = f"https://runbot.odoo.com/runbot/bundle/{first_attached_branch}"
        runbot_text = osc8(runbot_url, first_attached_branch)
        print(f"        runbot: {runbot_text}")
    for link_alias, link_url in github_links:
        link_padding = " " * (max_alias_len - len(link_alias) + 1)
        print(f"        {link_alias}:{link_padding}{osc8(link_url, link_url)}")

    print()
