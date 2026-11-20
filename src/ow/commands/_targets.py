"""Target resolution and repo expansion shared by `ow add` and `ow run`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedTarget
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _resolve_single_part(
    part: str,
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Ask plugins to resolve `part`; return the first non-None ResolvedTarget."""
    for _plugin, answer in plugins.call(
        "ow_resolve_target",
        target=part,
        known_repos=catalog,
        settings=settings,
    ):
        if isinstance(answer, ResolvedTarget):
            return answer
    return None


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
