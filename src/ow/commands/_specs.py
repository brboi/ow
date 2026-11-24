"""Unified spec DSL parsing, resolution, and repo expansion shared by `ow init`, `ow add`, and `ow run`."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ow.commands._repos import origin_has, origin_head
from ow.core import bases, git, stacks
from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedSpec
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace

SPEC_USAGE_HINT = (
    "pass a PR (enterprise#12345), base (@18.0), repo@branch, known repo (voip), "
    "Runbot URL, or register with `ow repo add <name> <url>`"
)

_BRANCH_SPEC_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+)@(?P<ref>.+)$")
_NAME_PREFIX_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)=(?P<spec>.+)$")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SSH_SCP_RE = re.compile(r"^git@[A-Za-z0-9._-]+:")
_HOST_PORT_BEFORE_RE = re.compile(r"(?:^|://|[=,]|\s)([A-Za-z0-9._-]+)$")
_HOST_PORT_AFTER_RE = re.compile(r"^\d+/")


@dataclass(frozen=True)
class UnitSpec:
    """A parsed CLI spec token: `[<name>=]<spec>[:<args>]`."""

    name: str | None
    spec: str
    raw: str
    extra_args: tuple[str, ...] = ()


def split_name_prefix(raw: str) -> tuple[str | None, str]:
    """Split an optional `<name>=<spec>` prefix without breaking URL query strings."""
    text = raw.strip()
    if not text or _URL_SCHEME_RE.match(text):
        return None, text
    match = _NAME_PREFIX_RE.match(text)
    if not match:
        return None, text
    name = stacks.validate_name(match.group("name"), kind="stack")
    spec = match.group("spec").strip()
    if not spec:
        raise UsageError(f"missing spec after '{name}=' in {raw!r}")
    return name, spec


def split_run_args(raw: str) -> tuple[str, tuple[str, ...]]:
    """Split `SPEC:"ARGS"` on the first `:` outside URL schemes, SCP SSH prefixes, and `host:port/`."""
    text = raw.strip()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == ":":
            if text[i : i + 3] == "://":
                i += 3
                continue
            # Skip `git@host:` in SSH repo URLs
            if _SSH_SCP_RE.match(text[: i + 1]):
                i += 1
                continue
            # Skip `host:port/...` (with or without URL scheme)
            if _HOST_PORT_BEFORE_RE.search(text[:i]) and _HOST_PORT_AFTER_RE.match(text[i + 1 :]):
                i += 1
                continue
            spec_part = text[:i].strip()
            args_part = text[i + 1 :].strip()
            if not spec_part:
                raise UsageError(f"missing spec before ':' in {raw!r}")
            if not args_part:
                return spec_part, ()
            try:
                parsed_args = tuple(shlex.split(args_part))
            except ValueError as exc:
                raise UsageError(f"invalid arguments in {raw!r}: {exc}") from exc
            return spec_part, parsed_args
        i += 1
    return text, ()


def parse_unit_spec(raw: str, *, allow_run_args: bool = False) -> UnitSpec:
    """Parse a raw CLI spec token into a `UnitSpec`."""
    if allow_run_args:
        spec_part, extra_args = split_run_args(raw)
    else:
        spec_part, extra_args = raw.strip(), ()
    name, spec = split_name_prefix(spec_part)
    return UnitSpec(name=name, spec=spec, raw=raw, extra_args=extra_args)


def deduplicate_name(candidate: str, taken: set[str]) -> str:
    """Return `candidate` if unused in `taken`, else `candidate-2`, `candidate-3`, ..."""
    cleaned = stacks.validate_name(candidate, kind="stack")
    if cleaned not in taken:
        taken.add(cleaned)
        return cleaned
    idx = 2
    while f"{cleaned}-{idx}" in taken:
        idx += 1
    unique = f"{cleaned}-{idx}"
    taken.add(unique)
    return unique


def _split_remote_prefix(ref: str, remotes: Sequence[str]) -> tuple[str | None, str]:
    """Split `<remote>/<branch>` by the longest matching remote name in `remotes`."""
    best = ""
    for remote in remotes:
        if ref.startswith(remote + "/") and len(remote) > len(best):
            best = remote
    if best:
        return best, ref[len(best) + 1 :]
    return None, ref


def _is_upstream_base_branch(
    bare: Path,
    repo: str,
    remote: str | None,
    branch: str,
    plugins: Plugins,
    ws: Workspace | None,
) -> bool:
    """Whether `<remote>/<branch>` is an upstream base/series branch (checked out detached)."""
    if remote not in ("origin", None):
        return False
    if bases.is_base_branch_name(branch):
        return True
    if plugins.first("ow_is_base_branch", ws=ws, repo=repo, branch=branch) is True:
        return True
    if bare.is_dir():
        head = origin_head(bare)
        if head and branch == head:
            return True
    return False


def _resolve_branch_spec(
    part: str,
    catalog: Mapping[str, KnownRepo],
    plugins: Plugins,
    ws: Workspace | None,
) -> ResolvedSpec | None:
    """Resolve `<repo>@<ref>` (or `<repo>@<remote>/<ref>`) across configured remotes.

    Upstream base/series branches (`master`, `main`, plugin base series, `origin/HEAD`)
    resolve detached (`branch=None, base=origin/<ref>`) to prevent cross-worktree branch
    collisions; feature branches resolve as local tracking branches.
    """
    match = _BRANCH_SPEC_RE.match(part.strip())
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
            if not git.remote_has_branch_or_cached(bare, explicit_remote, branch):
                raise UsageError(
                    f"branch {branch!r} not found on remote {explicit_remote!r} of {repo}"
                )
            chosen_remote = explicit_remote
        else:
            matches: list[str] = []
            if len(configured) == 1:
                if git.remote_has_branch_or_cached(bare, configured[0], branch):
                    matches.append(configured[0])
            elif configured:
                with ThreadPoolExecutor(max_workers=min(4, len(configured))) as pool:
                    results = list(
                        pool.map(
                            lambda r: (r, git.remote_has_branch_or_cached(bare, r, branch)),
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
            elif git.has_ref(bare, f"refs/heads/{branch}"):
                chosen_remote = None
            else:
                checked = ", ".join(configured) or "(none)"
                raise UsageError(
                    f"branch {branch!r} not found on any remote of {repo} (checked: {checked})"
                )

    is_base = _is_upstream_base_branch(bare, repo, chosen_remote, branch, plugins, ws)
    source = f"git:{repo}@{chosen_remote}/{branch}" if chosen_remote else f"git:{repo}@{branch}"
    if is_base:
        qualified_base = f"{chosen_remote or 'origin'}/{branch}"
        resolved_repo = ResolvedRepo(
            name=repo,
            url=url,
            branch=None,
            base=qualified_base,
            remote=None,
            explicit=True,
        )
    else:
        resolved_repo = ResolvedRepo(
            name=repo,
            url=url,
            branch=branch,
            base=None,
            remote=chosen_remote,
            explicit=True,
        )

    return ResolvedSpec(
        name=stacks.sanitize_slug(branch, repo),
        repos=(resolved_repo,),
        source=source,
    )


def _resolve_single_part(
    part: str,
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
    ws: Workspace | None,
) -> ResolvedSpec | None:
    """Ask plugins to resolve `part` (PRs, Runbot URLs, `@<base>`), then handle `<repo>@<ref>`."""
    answer = plugins.first(
        "ow_resolve_spec",
        ws=ws,
        spec=part,
        known_repos=catalog,
        settings=settings,
    )
    if isinstance(answer, ResolvedSpec):
        return answer
    text = part.strip()
    if text.startswith("@"):
        raw_base = text[1:].strip() or "<base>"
        raise UsageError(
            f"cannot expand {text!r}: no enabled plugin handles @<base> specs "
            f"(enable odoo plugin or specify <repo>@{raw_base})"
        )
    return _resolve_branch_spec(part, catalog, plugins, ws)


def expand_repos(
    repos: Sequence[ResolvedRepo],
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
    ws: Workspace | None = None,
) -> tuple[ResolvedRepo, ...]:
    """Pipe `repos` sequentially through every `ow_expand_workspace_repos` hook enabled for `ws`."""
    result = plugins.pipe(
        "ow_expand_workspace_repos",
        "repos",
        tuple(repos),
        ws=ws,
        known_repos=catalog,
        settings=settings,
    )
    return tuple(result)


def _is_known_or_registered_repo(name: str, catalog: Mapping[str, KnownRepo]) -> bool:
    return (repos_dir() / f"{name}.git").is_dir() or name in catalog


def _shared_base_applies(repo_name: str, shared_base: str | None) -> str | None:
    """Use `shared_base` for a bare repo only when its `origin` actually has that branch."""
    if shared_base is None:
        return None
    bare = repos_dir() / f"{repo_name}.git"
    if not bare.is_dir():
        return shared_base
    branch = shared_base.split("/", 1)[-1]
    if origin_has(bare, branch):
        return shared_base
    return None


def resolve_composite_spec(
    spec: str,
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
    ws: Workspace | None = None,
) -> ResolvedSpec | None:
    """Resolve a spec or `,`-joined composite spec.

    Supports:
    - PRs (`enterprise#12345`, `odoo/iap-apps#42`, `enterprise/pull/12345`, PR URL)
    - Runbot URLs (`https://runbot.odoo.com/runbot/bundle/12345`, `.../batch/98765`)
    - Base shorthand (`@18.0`, `@master`)
    - Repo + ref (`iap@18.0`, `enterprise@18.0-my-fix`, `voip@dev/my-branch`)
    - Bare repo (`voip`, `upgrade`)
    """
    raw_parts = [p.strip() for p in spec.split(",")]
    if any(not p for p in raw_parts):
        if len(raw_parts) == 1:
            return None
        raise UsageError(f"empty item in composite spec {spec!r}")
    parts = raw_parts

    resolved_parts: list[ResolvedSpec] = []
    extra_repo_names: list[str] = []
    unknown_parts: list[str] = []

    for part in parts:
        res = _resolve_single_part(part, plugins, catalog, settings, ws)
        if res is not None:
            resolved_parts.append(res)
        elif _is_known_or_registered_repo(part, catalog):
            extra_repo_names.append(part)
        else:
            unknown_parts.append(part)

    if unknown_parts:
        if len(parts) == 1:
            return None
        raise UsageError(f"unknown spec or repo {unknown_parts[0]!r} in {spec!r}")

    if not resolved_parts and not extra_repo_names:
        return None

    # Merge repos: explicit specs (`repo.explicit=True`) override auto-discovered companions/sisters
    # (`repo.explicit=False`); reject two explicit specs pointing at the same repo.
    merged_repos: dict[str, ResolvedRepo] = {}
    primary_repos: set[str] = set()
    sources: list[str] = []

    for res in resolved_parts:
        sources.append(res.source)
        for repo in res.repos:
            if not repo.explicit:
                merged_repos.setdefault(repo.name, repo)

    for res in resolved_parts:
        for primary in res.repos:
            if not primary.explicit:
                continue
            if primary.name in primary_repos:
                raise UsageError(
                    f"composite spec {spec!r} specifies repo {primary.name!r} more than once"
                )
            primary_repos.add(primary.name)
            merged_repos[primary.name] = primary

    shared_base = next((r.base for r in merged_repos.values() if r.base), None)
    for name in extra_repo_names:
        if name in primary_repos:
            raise UsageError(
                f"composite spec {spec!r} specifies repo {name!r} more than once"
            )
        if name not in merged_repos:
            known = catalog.get(name)
            bare_base = _shared_base_applies(name, shared_base)
            merged_repos[name] = ResolvedRepo(
                name=name,
                url=known.url if known else None,
                branch=None,
                base=bare_base,
                explicit=True,
            )
            sources.append(f"repo:{name}")

    repos_tuple = expand_repos(tuple(merged_repos.values()), plugins, catalog, settings, ws)

    if resolved_parts:
        default_name = resolved_parts[0].name
    else:
        default_name = extra_repo_names[0]

    return ResolvedSpec(
        name=default_name,
        repos=repos_tuple,
        source=",".join(sources),
    )
