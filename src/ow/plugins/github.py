"""GitHub CLI (`gh`) plugin: PR spec resolution, sister-PR discovery, PR status & prune."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ow.core import bases, cache, config, git, stacks
from ow.core.paths import repos_dir
from ow.core.plugins import (
    KnownRepo,
    PruneCandidate,
    ResolvedRepo,
    ResolvedSpec,
    Setting,
    StatusInfo,
    hookimpl,
)
from ow.errors import UsageError
from ow.plugins._github import (
    BareCatalog,
    bare_gh_info,
    parse_gh_slug,
    parse_pr_spec,
    pr_identity,
    recorded_branch_pr,
)

if TYPE_CHECKING:
    from ow.core.facts import RepoFacts
    from ow.core.workspace import Workspace

_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,headRefName,baseRefName,"
    "headRepository,headRepositoryOwner,author"
)
_PR_STATUS_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,baseRefName,headRefName,headRepositoryOwner"
)
_GH_TIMEOUT_SECONDS = 15


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide GitHub plugin settings."""
    return [
        Setting(
            key="github.default_org",
            type=str,
            default="odoo",
            help="Default GitHub org for short PR specs (e.g. enterprise#12345)",
        ),
        Setting(
            key="github.cache_ttl",
            type=int,
            default=120,
            help="Cache TTL in seconds for GitHub PR status lookups",
        ),
    ]


def _run_gh(argv: Sequence[str], *, strict: bool) -> Any | None:
    """Run `gh <argv>` and parse JSON stdout; raises UsageError when `strict` else returns None."""
    env = os.environ.copy()
    env.setdefault("GH_PROMPT_DISABLED", "1")
    try:
        result = subprocess.run(
            ["gh", *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError:
        if strict:
            raise UsageError(
                "`gh` (GitHub CLI) is not installed; install it or disable with `ow config set --global github.enabled false`"
            )
        return None
    except subprocess.TimeoutExpired:
        if strict:
            raise UsageError(f"`gh {' '.join(argv[:3])}` timed out after {_GH_TIMEOUT_SECONDS}s")
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


def _login_of(pr: Mapping[str, Any], field: str) -> str | None:
    val = pr.get(field)
    if isinstance(val, Mapping):
        login = val.get("login")
        if isinstance(login, str) and login:
            return login.lower()
    return None


def _find_sister_pr(
    known: KnownRepo,
    head_ref: str,
    target_owner: str,
    bare_catalog: BareCatalog,
    *,
    primary_state: str,
    primary_head_owner: str | None,
    primary_author: str | None,
    primary_base_ref: str | None,
) -> ResolvedRepo | None:
    """Query GitHub for a matching sister PR on `known` with the same `headRefName`."""
    slug = parse_gh_slug(known.url)
    if slug is None or slug[0].lower() != target_owner.lower():
        return None
    gh_slug = f"{slug[0]}/{slug[1]}"
    state_arg = "open" if primary_state == "OPEN" else "all"
    data = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            gh_slug,
            "--head",
            head_ref,
            "--state",
            state_arg,
            "--limit",
            "5",
            "--json",
            _PR_VIEW_FIELDS,
        ],
        strict=False,
    )
    if not isinstance(data, list) or not data:
        return None

    matched: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cand_head_owner = _login_of(item, "headRepositoryOwner")
        if primary_head_owner and cand_head_owner and cand_head_owner != primary_head_owner:
            continue
        cand_author = _login_of(item, "author")
        if primary_author and cand_author and cand_author != primary_author:
            continue
        matched.append(item)

    if not matched:
        return None

    # Prefer a sister PR targeting the same base branch when multiple exist.
    pr = next(
        (m for m in matched if primary_base_ref and m.get("baseRefName") == primary_base_ref),
        matched[0],
    )
    number = config.as_int(pr.get("number"))
    base_ref = pr.get("baseRefName")
    if number is None or not isinstance(base_ref, str):
        return None

    local_name = known.name
    clone_url = known.url
    if bare_catalog.by_name(local_name) is None:
        existing = bare_catalog.find_bare_for_slug(slug[0].lower(), slug[1].lower())
        if existing is not None:
            local_name, clone_url = existing[0], existing[3]
    remote_hint = bare_catalog.remote_for_slug(local_name, gh_slug)
    return ResolvedRepo(
        name=local_name,
        url=clone_url,
        branch=head_ref,
        base=f"{remote_hint or 'origin'}/{base_ref}",
        remote=remote_hint,
        fetch_ref=f"refs/pull/{number}/head",
        explicit=False,
        pr=pr_identity(gh_slug, number),
    )


@hookimpl
def ow_resolve_spec(
    spec: str,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedSpec | None:
    """Resolve a GitHub PR spec (short form or URL) into a workspace creation plan."""
    parsed = parse_pr_spec(spec)
    if parsed is None:
        return None

    owner, repo, number = parsed
    default_org = cast(str, settings["github.default_org"])
    bare_catalog = BareCatalog.scan()
    local_name, gh_owner, gh_repo, clone_url = bare_catalog.resolve_identity(
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

    primary_state = str(pr.get("state") or "OPEN").upper()
    primary_head_owner = _login_of(pr, "headRepositoryOwner")
    primary_author = _login_of(pr, "author")
    remote_hint = bare_catalog.remote_for_slug(local_name, gh_slug)

    primary = ResolvedRepo(
        name=local_name,
        url=clone_url,
        branch=head_ref,
        base=f"{remote_hint or 'origin'}/{base_ref}",
        remote=remote_hint,
        fetch_ref=f"refs/pull/{number}/head",
        explicit=True,
        pr=pr_identity(gh_slug, number),
    )

    # Discover sister PRs across other known repos in the same org in parallel.
    # Deduplicate by GitHub slug and exclude the primary PR's own slug so a local bare
    # with a different name (e.g. `iap` -> `odoo/iap-apps`) never re-matches `iap-apps`.
    seen_slugs = {gh_slug.lower()}
    candidates: list[KnownRepo] = []
    for k in known_repos.values():
        if k.name == local_name:
            continue
        k_slug = parse_gh_slug(k.url)
        if k_slug is not None:
            slug_key = f"{k_slug[0]}/{k_slug[1]}".lower()
            if slug_key in seen_slugs:
                continue
            seen_slugs.add(slug_key)
        candidates.append(k)
    sisters: list[ResolvedRepo] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = [
                pool.submit(
                    _find_sister_pr,
                    known,
                    head_ref,
                    gh_owner,
                    bare_catalog,
                    primary_state=primary_state,
                    primary_head_owner=primary_head_owner,
                    primary_author=primary_author,
                    primary_base_ref=base_ref,
                )
                for known in candidates
            ]
            for fut in futures:
                found = fut.result()
                if found is not None:
                    sisters.append(found)

    ws_name = stacks.sanitize_slug(head_ref, "pr")
    return ResolvedSpec(
        name=ws_name,
        repos=(primary, *sisters),
        source=f"github:{gh_slug}#{number}",
    )


def _lookup_pr_by_number(
    gh_slug: str,
    number: int,
    *,
    ttl: int,
    offline: bool,
    strict: bool = False,
) -> dict[str, Any] | None:
    """Look up a specific PR by number using the TTL cache."""
    key = f"{gh_slug}#{number}"
    if ttl > 0 or offline:
        cached = cache.get("github_pr_view", key, ttl=ttl, allow_expired=offline)
        if cached is not None:
            return cached.get("pr") if isinstance(cached, dict) else None
    if offline:
        return None
    pr = _run_gh(
        ["pr", "view", str(number), "--repo", gh_slug, "--json", _PR_STATUS_FIELDS],
        strict=strict,
    )
    if not isinstance(pr, dict):
        return None
    cache.set("github_pr_view", key, {"pr": pr})
    return pr


def _branch_head_owner(bare: Path, branch: str) -> str | None:
    """Infer the GitHub fork owner from `branch.<branch>.remote`'s URL (e.g. `dev` -> `odoo-dev`)."""
    remote_res = git.run(["git", "-C", str(bare), "config", "--get", f"branch.{branch}.remote"])
    if remote_res.returncode != 0:
        return None
    remote = remote_res.stdout.strip()
    if not remote:
        return None
    url_res = git.run(["git", "-C", str(bare), "config", "--get", f"remote.{remote}.url"])
    if url_res.returncode != 0:
        return None
    slug = parse_gh_slug(url_res.stdout.strip())
    return slug[0].lower() if slug is not None else None


def _lookup_branch_pr(
    gh_slug: str,
    branch: str,
    *,
    ttl: int,
    offline: bool,
    head_owner: str | None = None,
) -> dict[str, Any] | None:
    """Look up the latest PR for `(gh_slug, branch)` using the TTL cache, preferring `head_owner`."""
    key = f"{gh_slug}:{head_owner}:{branch}" if head_owner else f"{gh_slug}:{branch}"
    if ttl > 0 or offline:
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
            "5",
            "--json",
            _PR_STATUS_FIELDS,
        ],
        strict=False,
    )
    if not isinstance(data, list):
        return None
    candidates = [item for item in data if isinstance(item, dict)]
    pr: dict[str, Any] | None = None
    if candidates:
        if head_owner:
            pr = next(
                (item for item in candidates if _login_of(item, "headRepositoryOwner") == head_owner),
                candidates[0],
            )
        else:
            pr = candidates[0]
    cache.set("github_pr", key, {"pr": pr})
    return pr


def _lookup_repo_pr(
    bare: Path,
    branch: str,
    base: str | None,
    *,
    ttl: int,
    offline: bool,
) -> dict[str, Any] | None:
    """Look up a repo's PR, preferring recorded `branch.<b>.ow-pr` over branch-name search."""
    recorded = recorded_branch_pr(bare, branch)
    if recorded is not None:
        return _lookup_pr_by_number(recorded[0], recorded[1], ttl=ttl, offline=offline)
    if bases.is_base_branch_name(branch, base):
        return None
    info = bare_gh_info(bare)
    gh_slug = info.gh_slug if info is not None else None
    if gh_slug is None:
        return None
    head_owner = _branch_head_owner(bare, branch)
    return _lookup_branch_pr(gh_slug, branch, ttl=ttl, offline=offline, head_owner=head_owner)


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
    spec: str | None,
    offline: bool,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> list[StatusInfo]:
    """Contribute GitHub PR status for each repo branch (or an explicit PR spec)."""
    if spec is not None:
        parsed = parse_pr_spec(spec)
        if parsed is None:
            return []
        owner, repo, number = parsed
        default_org = cast(str, settings["github.default_org"])
        ttl = cast(int, settings["github.cache_ttl"])
        _local_name, gh_owner, gh_repo, _clone_url = BareCatalog.scan().resolve_identity(
            owner, repo, known_repos, default_org
        )
        gh_slug = f"{gh_owner}/{gh_repo}"
        pr = _lookup_pr_by_number(
            gh_slug, number, ttl=ttl, offline=offline, strict=not offline
        )
        if not isinstance(pr, dict):
            return []
        return [
            StatusInfo(
                plugin="github",
                scope="external",
                subject=f"{gh_slug}#{number}",
                summary=_format_pr_summary(pr),
            )
        ]

    if ws is None or not facts:
        return []

    ttl = cast(int, settings["github.cache_ttl"])
    queries = [
        (rf.name, rf.bare, rf.branch, rf.base)
        for rf in facts
        if rf.branch and not rf.problem
    ]
    if not queries:
        return []

    infos: list[StatusInfo] = []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        futures = [
            (
                name,
                pool.submit(_lookup_repo_pr, bare, branch, base, ttl=ttl, offline=offline),
            )
            for name, bare, branch, base in queries
        ]
        for name, fut in futures:
            pr = fut.result()
            if pr is not None:
                infos.append(
                    StatusInfo(
                        plugin="github",
                        scope="repo",
                        subject=name,
                        summary=_format_pr_summary(pr),
                    )
                )
    return infos


@hookimpl
def ow_prune_candidates(
    workspaces: Sequence[Workspace],
    offline: bool,
) -> list[PruneCandidate]:
    """Identify workspaces whose checked-out branches ALL belong to MERGED or CLOSED PRs."""
    if offline or not workspaces:
        return []

    eligible: list[tuple[Workspace, list[tuple[Path, str, str | None]]]] = []
    queries: list[tuple[Path, str, str | None]] = []
    seen_queries: set[tuple[Path, str, str | None]] = set()

    for ws in workspaces:
        if not ws.settings.get("github.enabled", True):
            continue
        branch_repos = [repo for repo in ws.repos if repo.branch is not None]
        if not branch_repos:
            continue
        ws_queries: list[tuple[Path, str, str | None]] = []
        valid = True
        for repo in branch_repos:
            assert repo.branch is not None
            bare = repos_dir() / f"{repo.name}.git"
            if not bare.is_dir():
                valid = False
                break
            q = (bare, repo.branch, repo.base)
            ws_queries.append(q)
            if q not in seen_queries:
                seen_queries.add(q)
                queries.append(q)
        if valid and ws_queries:
            eligible.append((ws, ws_queries))

    if not queries:
        return []

    # Verify PR states live (ttl=0) in parallel before proposing destructive removal.
    pr_by_query: dict[tuple[Path, str, str | None], dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        futures = [
            (q, pool.submit(_lookup_repo_pr, q[0], q[1], q[2], ttl=0, offline=False))
            for q in queries
        ]
        for q, fut in futures:
            pr_by_query[q] = fut.result()

    candidates: list[PruneCandidate] = []
    for ws, ws_queries in eligible:
        prs: list[dict[str, Any]] = []
        all_accounted = True
        for q in ws_queries:
            pr = pr_by_query.get(q)
            if pr is None:
                all_accounted = False
                break
            prs.append(pr)

        if not all_accounted or not prs:
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
                )
            )
    return candidates
