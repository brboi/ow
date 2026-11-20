"""GitHub CLI (`gh`) plugin: PR target resolution, sister-PR discovery, PR status & prune."""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ow.core import cache, git
from ow.core.plugins import (
    KnownRepo,
    PruneCandidate,
    ResolvedRepo,
    ResolvedTarget,
    Setting,
    StatusInfo,
    hookimpl,
)
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.facts import RepoFacts
    from ow.core.workspace import Workspace

_PR_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<num>\d+)(?:/.*)?$"
)
_PR_SHORT_RE = re.compile(
    r"^(?:(?P<owner>[A-Za-z0-9_.-]+)/)?(?P<repo>[A-Za-z0-9_.-]+)#(?P<num>\d+)$"
)
_GH_REMOTE_RE = re.compile(
    r"(?:git@github\.com:|https?://github\.com/|ssh://git@github\.com/)(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,headRefName,baseRefName,"
    "headRepository,headRepositoryOwner,author"
)
_PR_STATUS_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,baseRefName,headRefName"
)


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide GitHub plugin settings."""
    return [
        Setting(
            key="github.default_org",
            type=str,
            default="odoo",
            help="Default GitHub org for short PR targets (e.g. enterprise#12345)",
        ),
        Setting(
            key="github.cache_ttl",
            type=int,
            default=120,
            help="Cache TTL in seconds for GitHub PR status lookups",
        ),
    ]


def _parse_gh_slug(url: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from a GitHub SSH/HTTPS URL, or None."""
    match = _GH_REMOTE_RE.match(url.strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def parse_pr_target(target: str) -> tuple[str | None, str, int] | None:
    """Parse `<repo>#<num>`, `<owner>/<repo>#<num>`, or a GitHub PR URL into `(owner, repo, number)`."""
    text = target.strip()
    match = _PR_URL_RE.match(text) or _PR_SHORT_RE.match(text)
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("num"))


def _sanitize_branch(branch: str) -> str:
    """Turn a branch name into a filesystem-safe directory slug."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return cleaned or "pr"


def _run_gh(argv: Sequence[str], *, strict: bool) -> Any | None:
    """Run `gh <argv>` and parse JSON stdout; raises UsageError when `strict` else returns None."""
    try:
        result = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        if strict:
            raise UsageError(
                "`gh` (GitHub CLI) is not installed; install it or disable with `ow config set --global github.enabled false`"
            )
        return None
    except OSError as exc:
        if strict:
            raise UsageError(f"failed to run `gh`: {exc}")
        return None

    if result.returncode != 0:
        if strict:
            detail = (result.stderr or result.stdout).strip().splitlines()
            msg = detail[0] if detail else f"exit {result.returncode}"
            raise UsageError(f"`gh {' '.join(argv[:3])}` failed: {msg}")
        return None

    text = result.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        if strict:
            raise UsageError("`gh` returned invalid JSON")
        return None


def _resolve_repo_identity(
    owner: str | None,
    repo: str,
    known_repos: Mapping[str, KnownRepo],
    default_org: str,
) -> tuple[str, str, str, str]:
    """Resolve `(local_name, gh_owner, gh_repo, clone_url)` from a parsed PR target."""
    # 1. Exact canonical name or alias match when owner is absent or matches known URL
    for known in known_repos.values():
        slug = _parse_gh_slug(known.url)
        if owner is not None and slug == (owner, repo):
            return known.name, slug[0], slug[1], known.url
        if owner is None and (repo == known.name or repo in known.aliases):
            if slug is not None:
                return known.name, slug[0], slug[1], known.url
            return known.name, default_org, repo, known.url

    gh_owner = owner or default_org
    return repo, gh_owner, repo, f"git@github.com:{gh_owner}/{repo}.git"


def _find_sister_pr(
    known: KnownRepo, head_ref: str, target_owner: str
) -> ResolvedRepo | None:
    """Query GitHub for an open sister PR on `known` with the same `headRefName`."""
    slug = _parse_gh_slug(known.url)
    if slug is None or slug[0] != target_owner:
        return None
    gh_slug = f"{slug[0]}/{slug[1]}"
    data = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            gh_slug,
            "--head",
            head_ref,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            _PR_VIEW_FIELDS,
        ],
        strict=False,
    )
    if not isinstance(data, list) or not data:
        return None
    pr = data[0]
    number = pr.get("number")
    base_ref = pr.get("baseRefName")
    if not isinstance(number, int) or not isinstance(base_ref, str):
        return None
    return ResolvedRepo(
        name=known.name,
        url=known.url,
        branch=head_ref,
        base=f"origin/{base_ref}",
        fetch_ref=f"refs/pull/{number}/head",
        metadata={"pr": pr},
    )


@hookimpl
def ow_resolve_target(
    target: str,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Resolve a GitHub PR target (short form or URL) into a workspace creation plan."""
    parsed = parse_pr_target(target)
    if parsed is None:
        return None

    owner, repo, number = parsed
    default_org = str(settings.get("github.default_org") or "odoo")
    local_name, gh_owner, gh_repo, clone_url = _resolve_repo_identity(
        owner, repo, known_repos, default_org
    )
    gh_slug = f"{gh_owner}/{gh_repo}"

    pr = _run_gh(
        ["pr", "view", str(number), "--repo", gh_slug, "--json", _PR_VIEW_FIELDS],
        strict=True,
    )
    if not isinstance(pr, dict):
        raise UsageError(f"could not resolve PR {gh_slug}#{number}")

    head_ref = pr.get("headRefName")
    base_ref = pr.get("baseRefName")
    if not isinstance(head_ref, str) or not isinstance(base_ref, str):
        raise UsageError(f"PR {gh_slug}#{number} missing head/base branch info")

    primary = ResolvedRepo(
        name=local_name,
        url=clone_url,
        branch=head_ref,
        base=f"origin/{base_ref}",
        fetch_ref=f"refs/pull/{number}/head",
        metadata={"pr": pr},
    )

    # Discover sister PRs across other known repos in the same org in parallel
    candidates = [k for k in known_repos.values() if k.name != local_name]
    sisters: list[ResolvedRepo] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = [
                pool.submit(_find_sister_pr, known, head_ref, gh_owner)
                for known in candidates
            ]
            for fut in futures:
                found = fut.result()
                if found is not None:
                    sisters.append(found)

    ws_name = f"{number}-{_sanitize_branch(head_ref)}"
    return ResolvedTarget(
        name=ws_name,
        repos=(primary, *sisters),
        source=f"github:{gh_slug}#{number}",
        metadata={"pr": pr},
    )


def _repo_gh_slug(bare: Path, default_org: str | None = None) -> str | None:
    """Return `owner/repo` from `bare`'s `origin` remote URL, or `ow.github-repo` config."""
    override = git.run(["git", "-C", str(bare), "config", "--get", "ow.github-repo"])
    if override.returncode == 0 and "/" in override.stdout.strip():
        return override.stdout.strip()
    result = git.run(["git", "-C", str(bare), "config", "--get", "remote.origin.url"])
    if result.returncode != 0:
        return None
    slug = _parse_gh_slug(result.stdout.strip())
    return f"{slug[0]}/{slug[1]}" if slug is not None else None


def _lookup_branch_pr(
    gh_slug: str, branch: str, *, ttl: int, offline: bool
) -> dict[str, Any] | None:
    """Look up the latest PR for `(gh_slug, branch)` using the TTL cache."""
    key = f"{gh_slug}:{branch}"
    cached = cache.get("github_pr", key, ttl=ttl, allow_expired=offline)
    if cached is not None:
        return cached.get("pr") if isinstance(cached, dict) else None

    if offline:
        return None

    data = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            gh_slug,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            _PR_STATUS_FIELDS,
        ],
        strict=False,
    )
    if not isinstance(data, list):
        return None
    pr = data[0] if data else None
    cache.set("github_pr", key, {"pr": pr})
    return pr


def _format_pr_summary(pr: Mapping[str, Any]) -> str:
    number = pr.get("number")
    state = str(pr.get("state") or "UNKNOWN").upper()
    if state == "OPEN" and pr.get("isDraft"):
        state = "DRAFT"
    review = pr.get("reviewDecision")
    suffix = f" ({str(review).lower()})" if review else ""
    return f"#{number} {state}{suffix}"


@hookimpl
def ow_status(
    ws: Workspace | None,
    facts: Sequence[RepoFacts],
    target: str | None,
    offline: bool,
) -> list[StatusInfo]:
    """Contribute GitHub PR status for each repo branch (or an explicit PR target)."""
    if target is not None:
        parsed = parse_pr_target(target)
        if parsed is None:
            return []
        owner, repo, number = parsed
        gh_owner = owner or "odoo"
        gh_slug = f"{gh_owner}/{repo}"
        if offline:
            return []
        pr = _run_gh(
            ["pr", "view", str(number), "--repo", gh_slug, "--json", _PR_STATUS_FIELDS],
            strict=False,
        )
        if not isinstance(pr, dict):
            return []
        return [
            StatusInfo(
                plugin="github",
                scope="external",
                target=f"{gh_slug}#{number}",
                summary=_format_pr_summary(pr),
                data=pr,
            )
        ]

    if ws is None or not facts:
        return []

    ttl_raw = ws.settings.get("github.cache_ttl", 120)
    ttl = int(ttl_raw) if isinstance(ttl_raw, int) else 120

    queries: list[tuple[str, str, str]] = []
    for repo_facts in facts:
        if not repo_facts.branch or repo_facts.problem:
            continue
        gh_slug = _repo_gh_slug(repo_facts.bare)
        if gh_slug is not None:
            queries.append((repo_facts.name, gh_slug, repo_facts.branch))

    if not queries:
        return []

    infos: list[StatusInfo] = []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        futures = [
            (name, pool.submit(_lookup_branch_pr, slug, branch, ttl=ttl, offline=offline))
            for name, slug, branch in queries
        ]
        for name, fut in futures:
            pr = fut.result()
            if pr is not None:
                infos.append(
                    StatusInfo(
                        plugin="github",
                        scope="repo",
                        target=name,
                        summary=_format_pr_summary(pr),
                        data=pr,
                    )
                )
    return infos


@hookimpl
def ow_prune_candidates(
    workspaces: Sequence[Workspace],
    offline: bool,
) -> list[PruneCandidate]:
    """Identify workspaces whose tracked GitHub PRs are all MERGED or CLOSED."""
    if offline or not workspaces:
        return []

    from ow.core.paths import repos_dir

    candidates: list[PruneCandidate] = []
    for ws in workspaces:
        ttl_raw = ws.settings.get("github.cache_ttl", 120)
        ttl = int(ttl_raw) if isinstance(ttl_raw, int) else 120

        prs: list[dict[str, Any]] = []
        for repo in ws.repos:
            if not repo.branch:
                continue
            bare = repos_dir() / f"{repo.name}.git"
            if not bare.is_dir():
                continue
            gh_slug = _repo_gh_slug(bare)
            if gh_slug is None:
                continue
            pr = _lookup_branch_pr(gh_slug, repo.branch, ttl=ttl, offline=False)
            if pr is not None:
                prs.append(pr)

        if not prs:
            continue
        states = {str(pr.get("state") or "").upper() for pr in prs}
        if states and states <= {"MERGED", "CLOSED"}:
            labels = ", ".join(
                f"#{pr.get('number')} {str(pr.get('state', '')).lower()}" for pr in prs
            )
            candidates.append(
                PruneCandidate(
                    workspace=ws.root,
                    reason=f"PR {labels}",
                    data={"prs": prs},
                )
            )
    return candidates
