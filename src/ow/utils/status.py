"""Data-only workspace status: what `ow status` shows, without printing it.

The dashboard and the CLI share this module. `gather_workspace_status`
returns a `WorkspaceStatus` carrying every fact the renderers need; the
CLI's `cmd_status` feeds it to `_render_status`, and the dashboard feeds
it to its detail pane.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ow.utils import paths
from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.display import counts
from ow.utils.drift import DriftResult, check_all_drift
from ow.utils.git import (
    get_remote_url,
    get_rev_list_count,
    get_upstream,
    get_worktree_branch,
    get_worktree_head,
    parallel_per_repo,
    resolve_spec_local,
)
from ow.utils.refs import FetchOutcome, fetch_workspace_refs
from rich.markup import escape


# ---------------------------------------------------------------------------
# Helpers (moved from commands/status.py to avoid duplication)
# ---------------------------------------------------------------------------


def github_url_from_remote(remote_url: str) -> str | None:
    """Parse git remote URL to GitHub web URL."""
    ssh_match = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if ssh_match:
        return f"https://github.com/{ssh_match.group(1)}/{ssh_match.group(2)}"
    https_match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if https_match:
        return f"https://github.com/{https_match.group(1)}/{https_match.group(2)}"
    return None


def is_odoo_remote(remote_url: str | None) -> bool:
    """Runbot only knows bundles for the odoo organisation's repositories."""
    if remote_url is None:
        return False
    github_base = github_url_from_remote(remote_url)
    return github_base is not None and github_base.startswith("https://github.com/odoo/")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoStatus:
    """One repo's status, ready to render."""

    alias: str
    spec: BranchSpec
    state: Literal["ok", "not_applied", "unresolved", "error"]
    kind: Literal["detached", "tracking", "tracking_base", "local"] | None
    head_label: str | None        # current branch; None means HEAD is detached
    short_hash: str | None        # detached only
    base_ref: str | None          # resolved base, e.g. "origin/master"
    upstream: str | None
    primary: tuple[int, int] | None      # (behind, ahead) — same order as counts()
    secondary: tuple[int, int] | None    # parenthesised pair
    github_url: str | None
    runbot_branch: str | None
    fetch_failed: bool
    error: str | None


@dataclass(frozen=True)
class WorkspaceStatus:
    """Every repo's status for a workspace, plus drift."""

    ws_dir: Path
    repos: list[RepoStatus]
    drift: list[DriftResult]

    @property
    def runbot_branch(self) -> str | None:
        for r in self.repos:
            if r.runbot_branch:
                return r.runbot_branch
        return None


# ---------------------------------------------------------------------------
# Display helpers — byte-identical output, driven by RepoStatus
# ---------------------------------------------------------------------------


def _display_detached_status(rs: RepoStatus, max_alias_len: int) -> str:
    """Format status line for a detached worktree."""
    alias = rs.alias
    padding = " " * (max_alias_len - len(alias) + 1)
    behind, ahead = rs.primary  # type: ignore[misc]
    status = f"[bold]{rs.base_ref}[/] {counts(behind, ahead)} ([yellow]DETACHED[/]: {rs.short_hash})"
    return f"        {escape(alias)}:{padding}{status}"


def _display_attached_status(rs: RepoStatus, max_alias_len: int) -> str:
    """Format status line for an attached worktree."""
    alias = rs.alias
    padding = " " * (max_alias_len - len(alias) + 1)

    head_label = rs.head_label if rs.head_label else "[yellow]DETACHED[/]"

    if rs.kind == "tracking":
        behind_up, ahead_up = rs.primary  # type: ignore[misc]
        behind_base, ahead_base = rs.secondary  # type: ignore[misc]
        status = f"[bold]{rs.upstream}[/] {counts(behind_up, ahead_up)} ([bold]{rs.base_ref}[/] {counts(behind_base, ahead_base)})"
    elif rs.kind == "tracking_base":
        behind_up, ahead_up = rs.primary  # type: ignore[misc]
        status = f"[bold]{head_label}[/] [dim](local)[/] ([bold]{rs.upstream}[/] {counts(behind_up, ahead_up)})"
    else:  # local
        behind_base, ahead_base = rs.primary  # type: ignore[misc]
        status = f"[bold]{head_label}[/] [dim](local)[/] ([bold]{rs.base_ref}[/] {counts(behind_base, ahead_base)})"

    return f"        {escape(alias)}:{padding}{status}"


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def _resolve_offline(
    ws: WorkspaceConfig, ws_dir: Path, config: Config,
) -> dict[str, BranchSpec]:
    """Resolve specs from local bare repos without fetching."""
    bare_repos_dir = paths.repos_dir()
    resolved_specs: dict[str, BranchSpec] = {}
    for alias, spec in ws.repos.items():
        if not (ws_dir / alias).exists():
            continue
        bare_repo_path = bare_repos_dir / f"{alias}.git"
        if not bare_repo_path.exists():
            continue
        alias_remotes = config.remotes.get(alias, {})
        try:
            resolved = resolve_spec_local(bare_repo_path, spec, alias_remotes)
            resolved_specs[alias] = resolved
        except RuntimeError:
            pass
    return resolved_specs


def _gather_one_repo(
    alias: str,
    spec: BranchSpec,
    resolved: BranchSpec,
    worktree_path: Path,
    bare_repo: Path,
    fetch_failed: bool,
) -> RepoStatus:
    """Gather all display data for one repo (runs in parallel)."""
    remote_url = get_remote_url(bare_repo, resolved.remote)
    github_url: str | None = None
    runbot_branch: str | None = None

    if resolved.is_detached:
        short_hash, _ = get_worktree_head(worktree_path)
        ahead, behind = get_rev_list_count(worktree_path, "HEAD", resolved.base_ref)
        if remote_url:
            github_base = github_url_from_remote(remote_url)
            if github_base:
                github_url = f"{github_base}/commit/{short_hash}"
        return RepoStatus(
            alias=alias,
            spec=spec,
            state="ok",
            kind="detached",
            head_label=None,
            short_hash=short_hash,
            base_ref=resolved.base_ref,
            upstream=None,
            primary=(behind, ahead),
            secondary=None,
            github_url=github_url,
            runbot_branch=None,
            fetch_failed=fetch_failed,
            error=None,
        )

    # Attached
    actual_branch = get_worktree_branch(worktree_path)
    head_label = actual_branch  # may be None if detached at this point
    upstream = get_upstream(worktree_path)

    if remote_url:
        github_base = github_url_from_remote(remote_url)
        if github_base:
            github_url = f"{github_base}/tree/{resolved.local_branch}"
    if is_odoo_remote(remote_url):
        runbot_branch = resolved.local_branch

    if upstream:
        ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", upstream)
        if upstream != resolved.base_ref:
            ahead_base, behind_base = get_rev_list_count(worktree_path, upstream, resolved.base_ref)
            return RepoStatus(
                alias=alias, spec=spec, state="ok", kind="tracking",
                head_label=head_label, short_hash=None,
                base_ref=resolved.base_ref, upstream=upstream,
                primary=(behind_up, ahead_up),
                secondary=(behind_base, ahead_base),
                github_url=github_url, runbot_branch=runbot_branch,
                fetch_failed=fetch_failed, error=None,
            )
        else:
            return RepoStatus(
                alias=alias, spec=spec, state="ok", kind="tracking_base",
                head_label=head_label, short_hash=None,
                base_ref=resolved.base_ref, upstream=upstream,
                primary=(behind_up, ahead_up), secondary=None,
                github_url=github_url, runbot_branch=runbot_branch,
                fetch_failed=fetch_failed, error=None,
            )
    else:
        ahead_base, behind_base = get_rev_list_count(worktree_path, "HEAD", resolved.base_ref)
        return RepoStatus(
            alias=alias, spec=spec, state="ok", kind="local",
            head_label=head_label, short_hash=None,
            base_ref=resolved.base_ref, upstream=None,
            primary=(behind_base, ahead_base), secondary=None,
            github_url=github_url, runbot_branch=runbot_branch,
            fetch_failed=fetch_failed, error=None,
        )


def gather_workspace_status(
    ws: WorkspaceConfig, ws_dir: Path, config: Config, *, fetch: bool,
) -> WorkspaceStatus:
    """cmd_status's body minus every console.print. No output, no mutation."""
    drift = check_all_drift(ws, ws_dir)

    if fetch:
        fetched = fetch_workspace_refs(ws, ws_dir, config, fetch_upstreams=True)
    else:
        resolved_specs = _resolve_offline(ws, ws_dir, config)
        fetched = FetchOutcome(
            tracks={},
            upstreams={},
            specs=resolved_specs,
            upstream_before={},
            failed=frozenset(),
        )

    resolved_specs = fetched.specs
    bare_repos_dir = paths.repos_dir()

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
        ff = alias in fetched.failed
        status_tasks[alias] = (
            lambda a=alias, s=spec, r=resolved, w=worktree_path, b=bare_repo, f=ff:
            _gather_one_repo(a, s, r, w, b, f)
        )

    if status_tasks:
        status_results = parallel_per_repo(status_tasks)
    else:
        status_results = {}

    # Build RepoStatus list in config order
    repos: list[RepoStatus] = []
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            repos.append(RepoStatus(
                alias=alias, spec=spec, state="not_applied",
                kind=None, head_label=None, short_hash=None,
                base_ref=None, upstream=None, primary=None, secondary=None,
                github_url=None, runbot_branch=None,
                fetch_failed=False, error=None,
            ))
            continue

        resolved = resolved_specs.get(alias)
        if resolved is None:
            repos.append(RepoStatus(
                alias=alias, spec=spec, state="unresolved",
                kind=None, head_label=None, short_hash=None,
                base_ref=None, upstream=None, primary=None, secondary=None,
                github_url=None, runbot_branch=None,
                fetch_failed=False, error="could not resolve",
            ))
            continue

        result = status_results.get(alias)
        if isinstance(result, Exception):
            repos.append(RepoStatus(
                alias=alias, spec=spec, state="error",
                kind=None, head_label=None, short_hash=None,
                base_ref=None, upstream=None, primary=None, secondary=None,
                github_url=None, runbot_branch=None,
                fetch_failed=alias in fetched.failed, error=None,
            ))
            continue

        repos.append(result)

    return WorkspaceStatus(ws_dir=ws_dir, repos=repos, drift=drift)
