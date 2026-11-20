"""Odoo targets: `@<base>` resolution, series/base inference and companion core expansion."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from ow.core import bases, git, stacks
from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedTarget
from ow.errors import UsageError
from ow.plugins._github import bare_gh_info, parse_git_slug, parse_pr_identity
from ow.plugins._odoo_repos import (
    ODOO_ADDON_NAMES,
    ODOO_ADDON_SLUGS,
    ODOO_CORE,
    ODOO_CORE_NAMES,
    ODOO_CORE_SLUGS,
    ODOO_NAMES,
    ODOO_SLUGS,
    is_odoo_series,
)

_SERIES_PREFIX_RE = re.compile(r"^(?P<series>master|saas-\d+\.\d+|\d+\.\d+)(?:-|$)")


def _repo_slugs(repo: ResolvedRepo) -> set[str]:
    slugs: set[str] = set()
    parsed = parse_git_slug(repo.url)
    if parsed is not None:
        slugs.add(f"{parsed[0]}/{parsed[1]}")
    if repo.pr is not None and (parsed_pr := parse_pr_identity(repo.pr)) is not None:
        slugs.add(parsed_pr[0].lower())
    info = bare_gh_info(repos_dir() / f"{repo.name}.git")
    if info is not None and info.gh_slug:
        slugs.add(info.gh_slug.lower())
    return slugs


def _infer_odoo_base(repo: ResolvedRepo) -> str | None:
    """Infer `origin/<series>` from an Odoo branch name (e.g. `18.0-fix` -> `origin/18.0`)."""
    if repo.base is not None or not repo.branch:
        return None
    if repo.name not in ODOO_NAMES and not (_repo_slugs(repo) & ODOO_SLUGS):
        return None
    match = _SERIES_PREFIX_RE.match(repo.branch)
    if not match:
        return None
    series = match.group("series")
    bare = repos_dir() / f"{repo.name}.git"
    if bare.is_dir() and git.remote_has_branch_or_cached(bare, "origin", series, prefer_cache=True):
        return f"origin/{series}"
    return None


def resolve_target(
    target: str,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Resolve `@<base>` into `odoo.default_repos` detached on `<base>`."""
    part = target.strip()
    if not part.startswith("@"):
        return None
    raw_base = part[1:].strip()
    if not raw_base:
        raise UsageError(f"base ref cannot be empty in {part!r}")
    default_repos = tuple(dict.fromkeys(cast(list[str], settings["odoo.default_repos"])))
    if not default_repos:
        raise UsageError(
            f"cannot expand {part!r}: no default repos configured (set odoo.default_repos or specify <repo>@{raw_base})"
        )
    resolved_repos: list[ResolvedRepo] = []
    for rname in default_repos:
        known = known_repos.get(rname)
        bare = repos_dir() / f"{rname}.git"
        if not bare.is_dir() and known is None:
            raise UsageError(
                f"unknown default repo {rname!r}; register it with `ow repo add {rname} <url>`"
            )
        qualified_base = bases.parse_base_for(raw_base, rname)
        resolved_repos.append(
            ResolvedRepo(
                name=rname,
                url=known.url if known else None,
                branch=None,
                base=qualified_base,
                explicit=True,
            )
        )
    short_name = raw_base.split("/", 1)[-1]
    return ResolvedTarget(
        name=stacks.sanitize_slug(short_name, "workspace"),
        repos=tuple(resolved_repos),
        source=f"base:{raw_base}",
    )


def is_base_branch(branch: str) -> bool:
    """Whether `branch` is an Odoo release/series branch (`18.0`, `saas-18.3`, `master`, `main`)."""
    return is_odoo_series(branch)


def expand_workspace_repos(
    repos: tuple[ResolvedRepo, ...],
    known_repos: Mapping[str, KnownRepo],
) -> tuple[ResolvedRepo, ...] | None:
    """Infer Odoo series bases for branch targets and ensure addon targets get an Odoo core repo."""
    updated: list[ResolvedRepo] = []
    changed = False
    for r in repos:
        inferred = _infer_odoo_base(r)
        if inferred is not None:
            updated.append(replace(r, base=inferred))
            changed = True
        else:
            updated.append(r)

    names = {r.name for r in updated}
    slugs: set[str] = set()
    for r in updated:
        slugs.update(_repo_slugs(r))
    if names & ODOO_CORE_NAMES or slugs & ODOO_CORE_SLUGS:
        return tuple(updated) if changed else None
    if not (names & ODOO_ADDON_NAMES) and not (slugs & ODOO_ADDON_SLUGS):
        return tuple(updated) if changed else None

    base = next((r.base for r in updated if r.base), None)
    core_known = known_repos.get(ODOO_CORE.name)
    core_url = core_known.url if core_known is not None else ODOO_CORE.clone_url
    core_name = ODOO_CORE.name
    if not (repos_dir() / f"{ODOO_CORE.name}.git").is_dir() and (repos_dir() / f"{ODOO_CORE.repo}.git").is_dir():
        core_name = ODOO_CORE.repo
    core_repo = ResolvedRepo(name=core_name, url=core_url, branch=None, base=base, explicit=False)
    return (core_repo, *updated)
