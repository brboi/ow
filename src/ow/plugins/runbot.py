"""Odoo Runbot plugin: companion commit pinning, Runbot bundle/batch specs, and build status.

Uses Runbot 19.0's public unauthenticated JSON API (`/runbot/api/bundle` and `/runbot/api/batch`).
Network or lookup failures fall back cleanly during implicit expansion/status so offline
workflows are never blocked.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from ow import term
from ow.core import bases, cache, config, git, stacks
from ow.core.paths import repos_dir
from ow.plugin_api import (
    KnownRepo,
    RepoFacts,
    ResolvedRepo,
    ResolvedSpec,
    Setting,
    StatusInfo,
    UsageError,
    Workspace,
    hookimpl,
)
from ow.plugins._github import (
    BareCatalog,
    bare_gh_info,
    parse_gh_slug,
    parse_pr_identity,
    parse_pr_spec,
    recorded_branch_pr,
)
from ow.plugins._odoo_repos import (
    ODOO_CORE,
    ODOO_ORG,
    is_odoo_repo,
    is_odoo_series,
    is_odoo_slug,
    odoo_clone_url,
    runbot_repo_name,
)

_DEFAULT_RUNBOT_URL = "https://runbot.odoo.com"
_RUNBOT_TIMEOUT_SECONDS = 8
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_RUNBOT_URL_RE = re.compile(
    r"^(?:https?://)?(?P<host>[A-Za-z0-9_.-]+(?::\d+)?)/runbot/(?P<kind>bundle|batch)/(?P<ident>[A-Za-z0-9._-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide Runbot plugin settings."""
    return [
        Setting(
            key="runbot.url",
            type=str,
            default=_DEFAULT_RUNBOT_URL,
            help="Base URL of the Odoo Runbot instance",
        ),
        Setting(
            key="runbot.cache_ttl",
            type=int,
            default=120,
            help="Cache TTL in seconds for Runbot API lookups",
        ),
    ]


def _urlopen(req: urllib.request.Request, timeout: float):
    """Thin wrapper around `urllib.request.urlopen`, stubbed in tests for network isolation."""
    return urllib.request.urlopen(req, timeout=timeout)


def _base_url_from(settings: Mapping[str, object] | None) -> str:
    raw = settings.get("runbot.url") if settings else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip().rstrip("/")
    return _DEFAULT_RUNBOT_URL


def _host_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    return (parsed.netloc or "").lower()


def parse_runbot_spec(spec: str, *, base_url: str = _DEFAULT_RUNBOT_URL) -> tuple[str, str] | None:
    """Parse a Runbot bundle/batch URL into `('bundle'|'batch', ident)`, or None."""
    match = _RUNBOT_URL_RE.match(spec.strip())
    if not match:
        return None
    host = match.group("host").lower()
    allowed_hosts = {_host_of(_DEFAULT_RUNBOT_URL), _host_of(base_url)}
    if host not in allowed_hosts:
        return None
    return match.group("kind").lower(), match.group("ident")


def _get_json(
    base_url: str,
    path: str,
    *,
    ttl: int = 0,
    offline: bool = False,
    strict: bool = False,
) -> tuple[Any | None, bool]:
    """GET JSON from Runbot's public API; returns `(data, reachable)`."""
    full_url = f"{base_url.rstrip('/')}{path}"
    if ttl > 0 or offline:
        cached = cache.get("runbot_api", full_url, ttl=ttl, allow_expired=offline)
        if cached is not None:
            return (cached.get("data") if isinstance(cached, dict) else None), True
    if offline:
        return None, True

    req = urllib.request.Request(
        full_url,
        headers={"Accept": "application/json", "User-Agent": "ow"},
    )
    try:
        with _urlopen(req, timeout=_RUNBOT_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        if strict:
            raise UsageError(f"Runbot request failed ({full_url}): {exc}") from exc
        return None, False

    try:
        data = json.loads(raw)
    except ValueError as exc:
        if strict:
            raise UsageError(f"Runbot returned invalid JSON ({full_url})") from exc
        return None, False

    if ttl > 0:
        cache.set("runbot_api", full_url, {"data": data})
    return data, True


def _runbot_repo_name(repo: ResolvedRepo) -> str:
    """Runbot's repo name for `repo`: from its PR identity or clone URL, else from its local name."""
    if repo.pr is not None and (parsed_pr := parse_pr_identity(repo.pr)) is not None:
        return parsed_pr[0].split("/", 1)[1].lower()
    if repo.url:
        parsed = parse_gh_slug(repo.url)
        if parsed is not None:
            return parsed[1].lower()
    return runbot_repo_name(repo.name)


def _local_repo_for_runbot_name(
    runbot_repo: str,
    known_repos: Mapping[str, KnownRepo],
    bare_catalog: BareCatalog,
) -> tuple[str, str | None]:
    """Map Runbot's repo name (`odoo`, `enterprise`, `iap-apps`, ...) to `(local_name, clone_url)`."""
    name_low = runbot_repo.lower()
    if name_low == ODOO_CORE.repo:
        for local_name in (ODOO_CORE.name, ODOO_CORE.repo):
            if (info := bare_catalog.by_name(local_name)) is not None:
                return local_name, info.clone_url or ODOO_CORE.clone_url
        known = known_repos.get(ODOO_CORE.name)
        return ODOO_CORE.name, known.url if known else ODOO_CORE.clone_url

    if (info := bare_catalog.by_name(runbot_repo)) is not None:
        return runbot_repo, info.clone_url

    existing = bare_catalog.find_bare_for_slug(ODOO_ORG, name_low)
    if existing is not None:
        return existing[0], existing[3]

    if runbot_repo in known_repos:
        return runbot_repo, known_repos[runbot_repo].url
    for k in known_repos.values():
        slug = parse_gh_slug(k.url)
        if slug is not None and slug[1].lower() == name_low:
            return k.name, k.url

    return runbot_repo, odoo_clone_url(runbot_repo)


def _pick_bundle(bundles: Any) -> dict[str, Any] | None:
    if not isinstance(bundles, list) or not bundles:
        return None
    dicts = [b for b in bundles if isinstance(b, dict)]
    if not dicts:
        return None
    with_batch = [b for b in dicts if b.get("last_batch_id")]
    if with_batch:
        return max(with_batch, key=lambda b: int(b.get("id") or 0))
    return max(dicts, key=lambda b: int(b.get("id") or 0))


def _get_batch(
    base_url: str,
    batch_id: int,
    *,
    ttl: int = 0,
    offline: bool = False,
    strict: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    data, reachable = _get_json(
        base_url,
        f"/runbot/api/batch/{batch_id}?depth=0&logs=none",
        ttl=ttl,
        offline=offline,
        strict=strict,
    )
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0], reachable
    return None, reachable


def _lookup_bundle_checked(
    base_url: str,
    pr_specs: Sequence[tuple[str, int]],
    branch: str | None,
    *,
    ttl: int = 0,
    offline: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    """Query `/runbot/api/bundle` by PR specs first, falling back to branch name."""
    any_reachable = False
    if pr_specs:
        pr_param = ",".join(f"{repo}:{num}" for repo, num in pr_specs)
        encoded = urllib.parse.quote(pr_param, safe=":,")
        data, reachable = _get_json(
            base_url,
            f"/runbot/api/bundle?pr={encoded}",
            ttl=ttl,
            offline=offline,
            strict=False,
        )
        any_reachable = any_reachable or reachable
        bundle = _pick_bundle(data)
        if bundle is not None:
            return bundle, True

    if branch and not is_odoo_series(branch):
        encoded_branch = urllib.parse.quote(branch, safe="")
        data, reachable = _get_json(
            base_url,
            f"/runbot/api/bundle/{encoded_branch}",
            ttl=ttl,
            offline=offline,
            strict=False,
        )
        any_reachable = any_reachable or reachable
        return _pick_bundle(data), any_reachable

    return None, any_reachable


def _bundle_last_batch_id(bundle: Mapping[str, Any]) -> int | None:
    if (last_id := config.as_int(bundle.get("last_batch_id"))) is not None and last_id > 0:
        return last_id
    batches = bundle.get("batches")
    if isinstance(batches, list):
        for entry in batches:
            if isinstance(entry, dict):
                if (bid := config.as_int(entry.get("id"))) is not None and bid > 0:
                    return bid
    return None


def _commits_by_repo(batch: Mapping[str, Any]) -> dict[str, str]:
    commits = batch.get("commits")
    if not isinstance(commits, list):
        return {}
    result: dict[str, str] = {}
    for entry in commits:
        if not isinstance(entry, dict):
            continue
        repo = entry.get("repo")
        sha = entry.get("sha")
        if isinstance(repo, str) and isinstance(sha, str) and _SHA40_RE.fullmatch(sha):
            result[repo.lower()] = sha
    return result


def _default_repos_set(settings: Mapping[str, object]) -> set[str]:
    """Local names of the Odoo repos Runbot may pin; none when the odoo plugin is disabled."""
    if not settings["odoo.enabled"]:
        return set()
    return set(cast(list[str], settings["odoo.default_repos"]))


def _bare_has_origin_branch(local_name: str, branch: str) -> bool:
    bare = repos_dir() / f"{local_name}.git"
    if not bare.is_dir():
        return True
    return git.remote_has_branch_or_cached(bare, "origin", branch, prefer_cache=True)


@hookimpl
def ow_resolve_spec(
    spec: str,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedSpec | None:
    """Resolve a Runbot bundle or batch URL into exact detached commits."""
    base_url = _base_url_from(settings)
    parsed = parse_runbot_spec(spec, base_url=base_url)
    if parsed is None:
        return None
    kind, ident = parsed

    bundle: dict[str, Any] | None = None
    batch: dict[str, Any] | None = None

    if kind == "bundle":
        encoded = urllib.parse.quote(ident, safe="")
        data, _ = _get_json(base_url, f"/runbot/api/bundle/{encoded}", strict=True)
        bundle = _pick_bundle(data)
        if bundle is None:
            raise UsageError(f"Runbot bundle {ident!r} not found")
        batch_id = _bundle_last_batch_id(bundle)
        if batch_id is None:
            raise UsageError(f"Runbot bundle {ident!r} has no batches yet")
        batch, _ = _get_batch(base_url, batch_id, strict=True)
        if batch is None:
            raise UsageError(f"Runbot batch {batch_id} not found")
    else:
        if not ident.isdigit():
            raise UsageError(f"invalid Runbot batch id {ident!r}")
        batch, _ = _get_batch(base_url, int(ident), strict=True)
        if batch is None:
            raise UsageError(f"Runbot batch {ident} not found")
        if (bundle_id := config.as_int(batch.get("bundle_id"))) is not None and bundle_id > 0:
            data, _ = _get_json(base_url, f"/runbot/api/bundle/{bundle_id}", strict=False)
            bundle = _pick_bundle(data)

    commits = _commits_by_repo(batch)
    if not commits:
        raise UsageError(f"Runbot {kind} {ident} lists no commits")

    bare_catalog = BareCatalog.scan()
    allowed = bare_catalog.bare_names() | _default_repos_set(settings)
    version = bundle.get("version") if isinstance(bundle, dict) else None
    version_str = version.strip() if isinstance(version, str) and version.strip() else None

    resolved_repos: list[ResolvedRepo] = []
    seen_local: set[str] = set()
    # Keep Odoo core first when present.
    ordered_items = sorted(
        commits.items(),
        key=lambda item: (0 if item[0] == ODOO_CORE.repo else 1, item[0]),
    )
    for rb_repo, sha in ordered_items:
        local_name, clone_url = _local_repo_for_runbot_name(rb_repo, known_repos, bare_catalog)
        if local_name not in allowed or local_name in seen_local:
            continue
        seen_local.add(local_name)
        repo_base: str | None = None
        if version_str and _bare_has_origin_branch(local_name, version_str):
            repo_base = f"origin/{version_str}"
        resolved_repos.append(
            ResolvedRepo(
                name=local_name,
                url=clone_url,
                branch=None,
                base=repo_base,
                commit=sha,
                explicit=True,
            )
        )

    if not resolved_repos:
        raise UsageError(
            f"Runbot {kind} {ident} matched none of your local/default repos "
            f"(available in batch: {', '.join(sorted(commits))})"
        )

    bundle_name = (
        (bundle.get("name") if isinstance(bundle, dict) else None)
        or batch.get("bundle_name")
        or f"{kind}-{ident}"
    )
    ws_name = stacks.sanitize_slug(str(bundle_name), f"{kind}-{ident}")

    return ResolvedSpec(
        name=ws_name,
        repos=tuple(resolved_repos),
        source=f"runbot:{kind}:{ident}",
    )


@hookimpl
def ow_expand_workspace_repos(
    repos: tuple[ResolvedRepo, ...],
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> tuple[ResolvedRepo, ...] | None:
    """Pin detached companion repos (e.g. `community`) to Runbot's exact `base_match` SHA."""
    detached_unpinned = [
        r for r in repos if r.branch is None and r.commit is None and r.fetch_ref is None
    ]
    if not detached_unpinned:
        return None

    pr_specs: list[tuple[str, int]] = []
    primary_branch: str | None = None
    for r in repos:
        parsed_pr = parse_pr_identity(r.pr) if r.pr else None
        if parsed_pr is not None:
            pr_specs.append((parsed_pr[0].split("/", 1)[1].lower(), parsed_pr[1]))
        if (
            primary_branch is None
            and r.branch is not None
            and is_odoo_repo(r.name, url=r.url, pr=r.pr)
            and not is_odoo_series(r.branch)
        ):
            primary_branch = r.branch

    if not pr_specs and primary_branch is None:
        return None

    base_url = _base_url_from(settings)
    ttl = cast(int, settings["runbot.cache_ttl"])
    bundle, reachable = _lookup_bundle_checked(
        base_url, pr_specs, primary_branch, ttl=ttl, offline=False
    )
    if not reachable:
        term.out("# runbot: unreachable, using base ref")
        return None
    if bundle is None:
        target_label = (
            ", ".join(f"{repo}:{num}" for repo, num in pr_specs)
            if pr_specs
            else (primary_branch or "target")
        )
        unpinned = ", ".join(r.name for r in detached_unpinned)
        term.warn(
            f"runbot: no public bundle found for {target_label} "
            f"(private project or not yet built); using base ref for {unpinned}"
        )
        return None

    batch_id = _bundle_last_batch_id(bundle)
    if batch_id is None:
        return None
    batch, batch_reachable = _get_batch(
        base_url, batch_id, ttl=ttl, offline=False, strict=False
    )
    if not batch_reachable:
        term.out("# runbot: unreachable, using base ref")
        return None
    if batch is None:
        return None

    commits = _commits_by_repo(batch)
    if not commits:
        return None

    bid = config.as_int(bundle.get("id"))
    bundle_label = f"runbot bundle #{bid}" if bid is not None else "runbot"

    updated: list[ResolvedRepo] = []
    changed = False
    for r in repos:
        if r.branch is None and r.commit is None and r.fetch_ref is None:
            rb_name = _runbot_repo_name(r)
            sha = commits.get(rb_name)
            if sha:
                term.out(f"# {r.name}: pinned to {sha[:10]} ({bundle_label})")
                updated.append(replace(r, commit=sha))
                changed = True
                continue
        updated.append(r)

    return tuple(updated) if changed else None


def _format_bundle_summary(bundle: Mapping[str, Any], batch: Mapping[str, Any] | None = None) -> str:
    bid = bundle.get("id")
    name = bundle.get("name") or ""
    state = ""
    result = ""
    if isinstance(batch, Mapping):
        state = str(batch.get("state") or "")
        result = str(batch.get("result") or "")
    else:
        batches = bundle.get("batches")
        if isinstance(batches, list) and batches and isinstance(batches[0], Mapping):
            state = str(batches[0].get("state") or "")
            result = str(batches[0].get("result") or "")
    status_bits = " ".join(part for part in (state, result) if part) or "unknown"
    label = f"bundle #{bid}" if bid else "bundle"
    if name:
        return f"{label} ({name}): {status_bits}"
    return f"{label}: {status_bits}"


def _facts_odoo_context(
    facts: Sequence[RepoFacts],
) -> tuple[list[tuple[str, int]], str | None]:
    """Extract `(pr_specs, feature_branch)` from Odoo repos in `facts`."""
    pr_specs: list[tuple[str, int]] = []
    branch_name: str | None = None
    for rf in facts:
        if not rf.branch or rf.problem:
            continue
        recorded = recorded_branch_pr(rf.bare, rf.branch)
        if recorded is not None:
            slug, num = recorded
            if is_odoo_slug(slug) or is_odoo_repo(rf.name):
                rb_repo = slug.split("/", 1)[1].lower() if "/" in slug else rf.name
                pr_specs.append((rb_repo, num))
                if branch_name is None:
                    branch_name = rf.branch
                continue
        if bases.is_base_branch_name(rf.branch, rf.base):
            continue
        info = bare_gh_info(rf.bare)
        gh_slug = info.gh_slug if info is not None else None
        if (gh_slug and is_odoo_slug(gh_slug)) or is_odoo_repo(rf.name):
            if branch_name is None:
                branch_name = rf.branch
    return pr_specs, branch_name


def _status_for_runbot_url(
    base_url: str,
    kind: str,
    ident: str,
    *,
    ttl: int,
    offline: bool,
) -> list[StatusInfo]:
    if kind == "bundle":
        encoded = urllib.parse.quote(ident, safe="")
        data, _ = _get_json(
            base_url,
            f"/runbot/api/bundle/{encoded}",
            ttl=ttl,
            offline=offline,
            strict=False,
        )
        bundle = _pick_bundle(data)
        if bundle is None:
            return []
        return [
            StatusInfo(
                plugin="runbot",
                scope="external",
                subject=f"bundle#{bundle.get('id', ident)}",
                summary=_format_bundle_summary(bundle),
            )
        ]
    if ident.isdigit():
        batch, _ = _get_batch(base_url, int(ident), ttl=ttl, offline=offline, strict=False)
        if batch is None:
            return []
        summary = (
            f"batch #{batch.get('id', ident)} ({batch.get('bundle_name', '')}): "
            f"{batch.get('state', '')} {batch.get('result', '')}"
        ).strip()
        return [
            StatusInfo(
                plugin="runbot",
                scope="external",
                subject=f"batch#{batch.get('id', ident)}",
                summary=summary,
            )
        ]
    return []


def _status_for_pr(
    base_url: str,
    spec: str,
    *,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
    ttl: int,
    offline: bool,
) -> list[StatusInfo]:
    parsed_pr = parse_pr_spec(spec)
    if parsed_pr is None:
        return []
    owner, repo, num = parsed_pr
    default_org = cast(str, settings["github.default_org"])
    _local, gh_owner, gh_repo, _url = BareCatalog.scan().resolve_identity(
        owner, repo, known_repos, default_org
    )
    if not is_odoo_slug(f"{gh_owner}/{gh_repo}"):
        return []
    bundle, _reachable = _lookup_bundle_checked(
        base_url, [(gh_repo.lower(), num)], None, ttl=ttl, offline=offline
    )
    if bundle is None:
        return []
    return [
        StatusInfo(
            plugin="runbot",
            scope="external",
            subject=f"bundle#{bundle.get('id')}",
            summary=_format_bundle_summary(bundle),
        )
    ]


def _status_for_workspace(
    base_url: str,
    ws: Workspace,
    facts: Sequence[RepoFacts],
    *,
    ttl: int,
    offline: bool,
) -> list[StatusInfo]:
    pr_specs, branch_name = _facts_odoo_context(facts)
    if not pr_specs and branch_name is None:
        return []
    bundle, _reachable = _lookup_bundle_checked(
        base_url, pr_specs, branch_name, ttl=ttl, offline=offline
    )
    if bundle is None:
        return []
    return [
        StatusInfo(
            plugin="runbot",
            scope="workspace",
            subject=ws.name,
            summary=_format_bundle_summary(bundle),
        )
    ]


@hookimpl
def ow_status(
    ws: Workspace | None,
    facts: Sequence[RepoFacts],
    spec: str | None,
    offline: bool,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> list[StatusInfo]:
    """Contribute Runbot build status for workspaces, PR specs, or Runbot URLs."""
    base_url = _base_url_from(settings)
    ttl = cast(int, settings["runbot.cache_ttl"])

    if spec is not None:
        rb = parse_runbot_spec(spec, base_url=base_url)
        if rb is not None:
            return _status_for_runbot_url(base_url, rb[0], rb[1], ttl=ttl, offline=offline)
        return _status_for_pr(
            base_url,
            spec,
            known_repos=known_repos,
            settings=settings,
            ttl=ttl,
            offline=offline,
        )

    if ws is None or not facts:
        return []
    return _status_for_workspace(base_url, ws, facts, ttl=ttl, offline=offline)
