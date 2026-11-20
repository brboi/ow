"""Target resolution and repo expansion shared by `ow add` and `ow run`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from typing import TYPE_CHECKING

from ow.core import git
from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedTarget
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins

_BRANCH_TARGET_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+)@(?P<ref>.+)$")


def _sanitize_branch(branch: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return cleaned or fallback


def _has_ref(bare: Path, ref: str) -> bool:
    return git.run(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", ref]).returncode == 0


def _split_remote_prefix(ref: str, remotes: Sequence[str]) -> tuple[str | None, str]:
    """Split `<remote>/<branch>` by the longest matching remote name in `remotes`."""
    best = ""
    for remote in remotes:
        if ref.startswith(remote + "/") and len(remote) > len(best):
            best = remote
    if best:
        return best, ref[len(best) + 1 :]
    return None, ref


def _remote_has_branch_or_cached(bare: Path, remote: str, branch: str) -> bool:
    try:
        return git.remote_has_branch(bare, remote, branch)
    except git.GitFailed:
        return _has_ref(bare, f"refs/remotes/{remote}/{branch}")


def _resolve_branch_target(
    part: str,
    catalog: Mapping[str, KnownRepo],
) -> ResolvedTarget | None:
    """Resolve `<repo>@<branch>` (or `<repo>@<remote>/<branch>`) across configured remotes."""
    match = _BRANCH_TARGET_RE.match(part.strip())
    if not match:
        return None

    repo = match.group("repo")
    raw_ref = match.group("ref").strip()
    if not raw_ref:
        raise UsageError(f"branch name cannot be empty in {part!r}")

    bare = repos_dir() / f"{repo}.git"
    known = catalog.get(repo)
    if not bare.is_dir() and known is None:
        raise UsageError(
            f"unknown repo {repo!r}; pass a URL first with `ow repo add {repo} <url>`"
        )

    if bare.is_dir():
        configured = git.remotes(bare)
        explicit_remote, branch = _split_remote_prefix(raw_ref, configured)
    else:
        configured = ("origin",)
        explicit_remote, branch = _split_remote_prefix(raw_ref, configured)

    if (
        not branch
        or branch.startswith("-")
        or "@{" in branch
        or git.run(["git", "check-ref-format", f"refs/heads/{branch}"]).returncode != 0
    ):
        raise UsageError(f"invalid branch name {branch!r} in {part!r}")

    chosen_remote: str | None
    if not bare.is_dir():
        chosen_remote = explicit_remote or "origin"
        url = known.url if known is not None else None
    else:
        origin_url = git.read(["git", "-C", str(bare), "remote", "get-url", "origin"], check=False)
        url = origin_url or (known.url if known is not None else None)

        if explicit_remote is not None:
            if not _remote_has_branch_or_cached(bare, explicit_remote, branch):
                raise UsageError(
                    f"branch {branch!r} not found on remote {explicit_remote!r} of {repo}"
                )
            chosen_remote = explicit_remote
        else:
            matches: list[str] = []
            if len(configured) == 1:
                if _remote_has_branch_or_cached(bare, configured[0], branch):
                    matches.append(configured[0])
            elif configured:
                with ThreadPoolExecutor(max_workers=min(4, len(configured))) as pool:
                    results = list(
                        pool.map(
                            lambda r: (r, _remote_has_branch_or_cached(bare, r, branch)),
                            configured,
                        )
                    )
                matches = [r for r, ok in results if ok]

            if len(matches) == 1:
                chosen_remote = matches[0]
            elif len(matches) > 1:
                if "origin" in matches:
                    chosen_remote = "origin"
                else:
                    raise UsageError(
                        f"branch {branch!r} exists on multiple remotes of {repo} "
                        f"({', '.join(matches)}); specify {repo}@<remote>/{branch}"
                    )
            elif _has_ref(bare, f"refs/heads/{branch}"):
                chosen_remote = None
            else:
                checked = ", ".join(configured) or "(none)"
                raise UsageError(
                    f"branch {branch!r} not found on any remote of {repo} (checked: {checked})"
                )

    source = f"git:{repo}@{chosen_remote}/{branch}" if chosen_remote else f"git:{repo}@{branch}"
    return ResolvedTarget(
        name=_sanitize_branch(branch, repo),
        repos=(
            ResolvedRepo(
                name=repo,
                url=url,
                branch=branch,
                base=None,
                remote=chosen_remote,
            ),
        ),
        source=source,
    )


def _resolve_single_part(
    part: str,
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Ask plugins to resolve `part`, then fall back to `<repo>@<branch>` git targets."""
    for _plugin, answer in plugins.call(
        "ow_resolve_target",
        target=part,
        known_repos=catalog,
        settings=settings,
    ):
        if isinstance(answer, ResolvedTarget):
            return answer
    return _resolve_branch_target(part, catalog)


def expand_repos(
    repos: Sequence[ResolvedRepo],
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> tuple[ResolvedRepo, ...]:
    """Run every enabled `ow_expand_workspace_repos` hook over `repos`."""
    repos_tuple = tuple(repos)
    for _plugin, expanded in plugins.call(
        "ow_expand_workspace_repos",
        repos=repos_tuple,
        known_repos=catalog,
        settings=settings,
    ):
        if expanded is not None:
            repos_tuple = tuple(expanded)
    return repos_tuple


def resolve_composite_target(
    spec: str,
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Resolve a target or `+`-joined composite target (e.g. `iap-apps#42+enterprise`)."""
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if not parts:
        return None

    resolved_parts: list[ResolvedTarget] = []
    extra_repo_names: list[str] = []
    unknown_parts: list[str] = []

    for part in parts:
        res = _resolve_single_part(part, plugins, catalog, settings)
        if res is not None:
            resolved_parts.append(res)
        elif len(parts) > 1:
            if (repos_dir() / f"{part}.git").is_dir() or part in catalog:
                extra_repo_names.append(part)
            else:
                unknown_parts.append(part)
        else:
            return None

    if not resolved_parts:
        # If every `+`-separated piece was a known repo (e.g. `community+enterprise`),
        # warn clearly instead of creating a workspace literally named `community+enterprise`.
        if len(parts) > 1 and not unknown_parts and extra_repo_names:
            raise UsageError(
                f"composite target {spec!r} needs at least one PR/target (e.g. enterprise#12345+community); "
                "for fresh workspaces pass -r <repo>"
            )
        # Otherwise treat as a normal fresh workspace name containing `+` (e.g. `fix-a+b`).
        return None

    if unknown_parts:
        raise UsageError(f"unknown target or repo {unknown_parts[0]!r} in {spec!r}")

    # Merge repos giving explicit primary targets precedence over auto-discovered sister PRs,
    # while rejecting two explicit primary targets that point at the same repository.
    merged_repos: dict[str, ResolvedRepo] = {}
    primary_repos: set[str] = set()
    sources: list[str] = []
    for res in resolved_parts:
        sources.append(res.source)
        for repo in res.repos[1:]:
            merged_repos.setdefault(repo.name, repo)
    for res in resolved_parts:
        if res.repos:
            primary = res.repos[0]
            if primary.name in primary_repos:
                raise UsageError(
                    f"composite target {spec!r} specifies repo {primary.name!r} more than once"
                )
            primary_repos.add(primary.name)
            merged_repos[primary.name] = primary

    shared_base = next((r.base for r in merged_repos.values() if r.base), None)
    for name in extra_repo_names:
        if name not in merged_repos:
            known = catalog.get(name)
            merged_repos[name] = ResolvedRepo(
                name=name,
                url=known.url if known else None,
                branch=None,
                base=shared_base,
            )

    repos_tuple = expand_repos(tuple(merged_repos.values()), plugins, catalog, settings)

    return ResolvedTarget(
        name=resolved_parts[0].name,
        repos=repos_tuple,
        source="+".join(sources),
        metadata=dict(resolved_parts[0].metadata),
    )
