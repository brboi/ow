"""GitHub CLI (`gh`) plugin: PR target resolution, sister-PR discovery, PR status & prune."""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ow.core import cache, git
from ow.core.paths import repos_dir
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
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<num>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_PR_SHORT_RE = re.compile(
    r"^(?:(?P<owner>[A-Za-z0-9_.-]+)/)?(?P<repo>[A-Za-z0-9_.-]+)#(?P<num>\d+)$"
)
_GH_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https?://(?:[^/@]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

_PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,headRefName,baseRefName,"
    "headRepository,headRepositoryOwner,author"
)
_PR_STATUS_FIELDS = (
    "number,title,url,state,isDraft,reviewDecision,baseRefName,headRefName"
)
_GH_TIMEOUT_SECONDS = 15
_BASE_BRANCH_NAMES = frozenset({"main", "master"})


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


def _ttl_from(settings: Mapping[str, object] | None) -> int:
    if not settings:
        return 120
    raw = settings.get("github.cache_ttl", 120)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return 120


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


def _resolve_repo_identity(
    owner: str | None,
    repo: str,
    known_repos: Mapping[str, KnownRepo],
    default_org: str,
) -> tuple[str, str, str, str]:
    """Resolve `(local_name, gh_owner, gh_repo, clone_url)` from a parsed PR target."""
    owner_low = owner.lower() if owner is not None else None
    repo_low = repo.lower()
    for known in known_repos.values():
        slug = _parse_gh_slug(known.url)
        if owner_low is not None and slug is not None and (slug[0].lower(), slug[1].lower()) == (owner_low, repo_low):
            return known.name, slug[0], slug[1], known.url
        if owner is None and (repo == known.name or repo in known.aliases):
            if slug is not None:
                return known.name, slug[0], slug[1], known.url
            return known.name, default_org, repo, known.url

    gh_owner = owner or default_org
    return repo, gh_owner, repo, f"git@github.com:{gh_owner}/{repo}.git"


def _remote_for_slug(bare: Path, gh_slug: str) -> str | None:
    """Return the remote name in `bare` whose URL matches `gh_slug`, or None."""
    if not bare.is_dir():
        return None
    target = gh_slug.lower()
    for remote in git.remotes(bare):
        res = git.run(["git", "-C", str(bare), "config", "--get", f"remote.{remote}.url"])
        if res.returncode != 0:
            continue
        slug = _parse_gh_slug(res.stdout.strip())
        if slug is not None and f"{slug[0]}/{slug[1]}".lower() == target:
            return remote
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
    *,
    primary_state: str,
    primary_head_owner: str | None,
    primary_author: str | None,
    primary_base_ref: str | None,
) -> ResolvedRepo | None:
    """Query GitHub for a matching sister PR on `known` with the same `headRefName`."""
    slug = _parse_gh_slug(known.url)
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
    number = pr.get("number")
    base_ref = pr.get("baseRefName")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(base_ref, str):
        return None
    remote_hint = _remote_for_slug(repos_dir() / f"{known.name}.git", gh_slug)
    return ResolvedRepo(
        name=known.name,
        url=known.url,
        branch=head_ref,
        base=f"origin/{base_ref}",
        remote=remote_hint,
        fetch_ref=f"refs/pull/{number}/head",
        metadata={"pr": pr, "ow_pr": f"{gh_slug}#{number}"},
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

    primary_state = str(pr.get("state") or "OPEN").upper()
    primary_head_owner = _login_of(pr, "headRepositoryOwner")
    primary_author = _login_of(pr, "author")
    remote_hint = _remote_for_slug(repos_dir() / f"{local_name}.git", gh_slug)

    primary = ResolvedRepo(
        name=local_name,
        url=clone_url,
        branch=head_ref,
        base=f"origin/{base_ref}",
        remote=remote_hint,
        fetch_ref=f"refs/pull/{number}/head",
        metadata={"pr": pr, "ow_pr": f"{gh_slug}#{number}"},
    )

    # Discover sister PRs across other known repos in the same org in parallel
    candidates = [k for k in known_repos.values() if k.name != local_name]
    sisters: list[ResolvedRepo] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
            futures = [
                pool.submit(
                    _find_sister_pr,
                    known,
                    head_ref,
                    gh_owner,
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

    ws_name = f"{number}-{_sanitize_branch(head_ref)}"
    return ResolvedTarget(
        name=ws_name,
        repos=(primary, *sisters),
        source=f"github:{gh_slug}#{number}",
        metadata={"pr": pr},
    )


def _repo_gh_slug(bare: Path) -> str | None:
    """Return `owner/repo` from `ow.github-repo` config or `origin`/`upstream` remote URL."""
    override = git.run(["git", "-C", str(bare), "config", "--get", "ow.github-repo"])
    if override.returncode == 0 and "/" in override.stdout.strip():
        return override.stdout.strip()
    for remote in ("origin", "upstream"):
        result = git.run(["git", "-C", str(bare), "config", "--get", f"remote.{remote}.url"])
        if result.returncode != 0:
            continue
        slug = _parse_gh_slug(result.stdout.strip())
        if slug is not None:
            return f"{slug[0]}/{slug[1]}"
    return None


def _recorded_branch_pr(bare: Path, branch: str) -> tuple[str, int] | None:
    """Return `(gh_slug, number)` from `branch.<branch>.ow-pr` if recorded."""
    res = git.run(["git", "-C", str(bare), "config", "--get", f"branch.{branch}.ow-pr"])
    if res.returncode != 0:
        return None
    raw = res.stdout.strip()
    if "#" not in raw or "/" not in raw:
        return None
    slug, _, num_str = raw.rpartition("#")
    if num_str.isdigit():
        return slug, int(num_str)
    return None


def _is_base_branch(branch: str, base: str | None) -> bool:
    """Whether `branch` is a generic/base branch name that should not be PR-guessed."""
    if branch in _BASE_BRANCH_NAMES:
        return True
    if base:
        base_branch = base.split("/", 1)[-1]
        if branch == base_branch:
            return True
    return False


def _lookup_pr_by_number(
    gh_slug: str, number: int, *, ttl: int, offline: bool
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
        strict=False,
    )
    if not isinstance(pr, dict):
        return None
    cache.set("github_pr_view", key, {"pr": pr})
    return pr


def _lookup_branch_pr(
    gh_slug: str, branch: str, *, ttl: int, offline: bool
) -> dict[str, Any] | None:
    """Look up the latest PR for `(gh_slug, branch)` using the TTL cache."""
    key = f"{gh_slug}:{branch}"
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


def _lookup_repo_pr(
    bare: Path,
    branch: str,
    base: str | None,
    *,
    ttl: int,
    offline: bool,
) -> dict[str, Any] | None:
    """Look up a repo's PR, preferring recorded `branch.<b>.ow-pr` over branch-name search."""
    recorded = _recorded_branch_pr(bare, branch)
    if recorded is not None:
        return _lookup_pr_by_number(recorded[0], recorded[1], ttl=ttl, offline=offline)
    if _is_base_branch(branch, base):
        return None
    gh_slug = _repo_gh_slug(bare)
    if gh_slug is None:
        return None
    return _lookup_branch_pr(gh_slug, branch, ttl=ttl, offline=offline)


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
    known_repos: Mapping[str, KnownRepo] = (),  # type: ignore[assignment]
    settings: Mapping[str, object] = (),  # type: ignore[assignment]
) -> list[StatusInfo]:
    """Contribute GitHub PR status for each repo branch (or an explicit PR target)."""
    if target is not None:
        parsed = parse_pr_target(target)
        if parsed is None:
            return []
        owner, repo, number = parsed
        eff_settings = settings if isinstance(settings, Mapping) else {}
        eff_known = known_repos if isinstance(known_repos, Mapping) else {}
        default_org = str(eff_settings.get("github.default_org") or "odoo")
        ttl = _ttl_from(eff_settings)
        _local_name, gh_owner, gh_repo, _clone_url = _resolve_repo_identity(
            owner, repo, eff_known, default_org
        )
        gh_slug = f"{gh_owner}/{gh_repo}"
        pr = _lookup_pr_by_number(gh_slug, number, ttl=ttl, offline=offline)
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

    ttl = _ttl_from(ws.settings)

    queries: list[tuple[str, Path, str, str | None]] = []
    for repo_facts in facts:
        if not repo_facts.branch or repo_facts.problem:
            continue
        queries.append((repo_facts.name, repo_facts.bare, repo_facts.branch, repo_facts.base))

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
    """Identify workspaces whose checked-out branches ALL belong to MERGED or CLOSED PRs."""
    if offline or not workspaces:
        return []

    candidates: list[PruneCandidate] = []
    for ws in workspaces:
        if not ws.settings.get("github.enabled", True):
            continue

        branch_repos = [repo for repo in ws.repos if repo.branch is not None]
        if not branch_repos:
            continue

        prs: list[dict[str, Any]] = []
        all_accounted = True
        for repo in branch_repos:
            assert repo.branch is not None
            bare = repos_dir() / f"{repo.name}.git"
            if not bare.is_dir():
                all_accounted = False
                break
            # Always verify live (ttl=0) before proposing destructive removal.
            pr = _lookup_repo_pr(bare, repo.branch, repo.base, ttl=0, offline=False)
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
                    data={"prs": prs},
                )
            )
    return candidates
