"""GitHub support shared by plugins: slug and PR-spec parsing, bare-repository catalog.

Support module, not a plugin: nothing here is a hook implementation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core import git
from ow.core.paths import repos_dir

if TYPE_CHECKING:
    from ow.core.plugins import KnownRepo

_GH_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https?://(?:[^/@]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_GIT_SLUG_RE = re.compile(
    r"(?:[:/])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_PR_URL_RE = re.compile(
    r"^(?:https?://)?github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<num>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_PR_PATH_RE = re.compile(
    r"^(?!github\.com/)(?:(?P<owner>[A-Za-z0-9_.-]+)/)?(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<num>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_PR_SHORT_RE = re.compile(
    r"^(?:(?P<owner>[A-Za-z0-9_.-]+)/)?(?P<repo>[A-Za-z0-9_.-]+)#(?P<num>\d+)$"
)


def github_clone_url(owner: str, repo: str) -> str:
    """The SSH clone URL of GitHub repository `owner/repo`."""
    return f"git@github.com:{owner}/{repo}.git"


def parse_gh_slug(url: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from a GitHub SSH/HTTPS URL, or None."""
    match = _GH_REMOTE_RE.match(url.strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def parse_git_slug(url: str | None) -> tuple[str, str] | None:
    """Extract lowercase `(owner, repo)` from any SSH/HTTPS git remote URL, or None."""
    if not url:
        return None
    match = _GIT_SLUG_RE.search(url.strip())
    if not match:
        return None
    return match.group("owner").lower(), match.group("repo").lower()


def parse_pr_spec(spec: str) -> tuple[str | None, str, int] | None:
    """Parse `<repo>#<num>`, `<owner>/<repo>#<num>`, `<owner>/<repo>/pull/<num>`, or a GitHub PR URL."""
    text = spec.strip()
    match = _PR_URL_RE.match(text) or _PR_PATH_RE.match(text) or _PR_SHORT_RE.match(text)
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("num"))


def pr_identity(gh_slug: str, number: int) -> str:
    """The `owner/repo#num` identity core records in `branch.<b>.ow-pr`."""
    return f"{gh_slug}#{number}"


def parse_pr_identity(identity: str) -> tuple[str, int] | None:
    """Split a `owner/repo#num` identity into `(gh_slug, number)`, or None when malformed."""
    slug, sep, num_str = identity.strip().rpartition("#")
    if not sep or "/" not in slug or not num_str.isdigit():
        return None
    return slug, int(num_str)


def recorded_branch_pr(bare: Path, branch: str) -> tuple[str, int] | None:
    """Return `(gh_slug, number)` from `branch.<branch>.ow-pr` if recorded."""
    res = git.run(["git", "-C", str(bare), "config", "--get", f"branch.{branch}.ow-pr"])
    if res.returncode != 0:
        return None
    return parse_pr_identity(res.stdout)


def registered_bare_repos() -> tuple[tuple[str, Path], ...]:
    """Return sorted `(name, bare_path)` pairs for bare repos under `repos_dir()`."""
    directory = repos_dir()
    if not directory.is_dir():
        return ()
    result: list[tuple[str, Path]] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.name.endswith(".git") or not entry.is_dir():
            continue
        result.append((entry.stem, entry))
    return tuple(result)


@dataclass(frozen=True)
class BareInfo:
    """GitHub/remote metadata extracted from one registered bare repository."""

    name: str
    path: Path
    slug: tuple[str, str] | None
    clone_url: str | None
    remote_slugs: Mapping[str, tuple[str, str]]
    remote_urls: Mapping[str, str]

    @property
    def gh_slug(self) -> str | None:
        if self.slug is None:
            return None
        return f"{self.slug[0]}/{self.slug[1]}"

    def remote_for_slug(self, gh_slug: str) -> str | None:
        """Return the first remote in `self` whose URL points at `gh_slug` (case-insensitive)."""
        target = gh_slug.lower()
        for remote, r_slug in self.remote_slugs.items():
            if f"{r_slug[0]}/{r_slug[1]}".lower() == target:
                return remote
        return None


def bare_gh_info(bare: Path) -> BareInfo | None:
    """Read `ow.github-repo` and all `remote.<name>.url` entries for `bare` in one git call."""
    if not bare.is_dir():
        return None

    res = git.run(
        [
            "git",
            "-C",
            str(bare),
            "config",
            "--get-regexp",
            r"^(ow\.github-repo|remote\.[^.]+\.url)$",
        ]
    )
    override_slug: tuple[str, str] | None = None
    remote_urls: dict[str, str] = {}
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            if " " not in line:
                continue
            key, val = line.split(" ", 1)
            val = val.strip()
            if key == "ow.github-repo":
                if "/" in val:
                    gh_owner, gh_repo = val.split("/", 1)
                    override_slug = (gh_owner, gh_repo)
            elif key.startswith("remote.") and key.endswith(".url"):
                remote_name = key[len("remote.") : -len(".url")]
                remote_urls.setdefault(remote_name, val)

    # Order remotes so `origin`, `upstream` precede any others while preserving git config order.
    ordered_remotes: list[str] = []
    for preferred in ("origin", "upstream"):
        if preferred in remote_urls:
            ordered_remotes.append(preferred)
    for rname in remote_urls:
        if rname not in ordered_remotes:
            ordered_remotes.append(rname)

    remote_slugs: dict[str, tuple[str, str]] = {}
    for rname in ordered_remotes:
        parsed = parse_gh_slug(remote_urls[rname])
        if parsed is not None:
            remote_slugs[rname] = parsed

    primary_slug: tuple[str, str] | None = None
    clone_url: str | None = None
    if override_slug is not None:
        primary_slug = override_slug
        clone_url = remote_urls.get("origin") or github_clone_url(*override_slug)
    else:
        for rname in ordered_remotes:
            if rname in remote_slugs:
                primary_slug = remote_slugs[rname]
                clone_url = remote_urls[rname]
                break

    return BareInfo(
        name=bare.stem,
        path=bare,
        slug=primary_slug,
        clone_url=clone_url,
        remote_slugs=remote_slugs,
        remote_urls=remote_urls,
    )


class BareCatalog:
    """In-memory snapshot of registered bare repositories indexed by name and GitHub slug."""

    def __init__(self, entries: tuple[BareInfo, ...] = ()) -> None:
        self._by_name: dict[str, BareInfo] = {}
        self._by_slug: dict[tuple[str, str], list[BareInfo]] = {}
        for info in entries:
            self._by_name[info.name] = info
            seen_slugs: set[tuple[str, str]] = set()
            if info.slug is not None:
                key = (info.slug[0].lower(), info.slug[1].lower())
                seen_slugs.add(key)
                self._by_slug.setdefault(key, []).append(info)
            for r_slug in info.remote_slugs.values():
                key = (r_slug[0].lower(), r_slug[1].lower())
                if key not in seen_slugs:
                    seen_slugs.add(key)
                    self._by_slug.setdefault(key, []).append(info)

    @classmethod
    def scan(cls) -> BareCatalog:
        """Scan `repos_dir()` once and build a `BareCatalog`."""
        infos: list[BareInfo] = []
        for _name, bare_path in registered_bare_repos():
            info = bare_gh_info(bare_path)
            if info is not None:
                infos.append(info)
        return cls(tuple(infos))

    def bare_names(self) -> set[str]:
        return set(self._by_name)

    def by_name(self, name: str) -> BareInfo | None:
        return self._by_name.get(name)

    def by_slug(self, owner: str, repo: str) -> BareInfo | None:
        """Return the first bare matching `(owner, repo)` (preferring primary slug over secondary remote)."""
        key = (owner.lower(), repo.lower())
        matches = self._by_slug.get(key)
        if not matches:
            return None
        for info in matches:
            if info.slug is not None and (info.slug[0].lower(), info.slug[1].lower()) == key:
                return info
        return matches[0]

    def find_bare_for_slug(self, owner_low: str, repo_low: str) -> tuple[str, str, str, str] | None:
        """Return `(bare_name, gh_owner, gh_repo, clone_url)` for the first bare matching `(owner, repo)`."""
        info = self.by_slug(owner_low, repo_low)
        if info is None:
            return None
        key = (owner_low.lower(), repo_low.lower())
        if info.slug is not None and (info.slug[0].lower(), info.slug[1].lower()) == key:
            return info.name, info.slug[0], info.slug[1], info.clone_url or github_clone_url(*info.slug)
        clone_url = info.clone_url or github_clone_url(owner_low, repo_low)
        return info.name, owner_low, repo_low, clone_url

    def remote_for_slug(self, bare_name: str, gh_slug: str) -> str | None:
        """Return the remote name in `bare_name` pointing at `gh_slug`, or None."""
        info = self._by_name.get(bare_name)
        if info is None:
            info = bare_gh_info(repos_dir() / f"{bare_name}.git")
        return info.remote_for_slug(gh_slug) if info is not None else None

    def resolve_by_slug(
        self,
        owner: str,
        repo: str,
        known_repos: Mapping[str, KnownRepo],
    ) -> tuple[str, str, str, str]:
        """Resolve `(local_name, gh_owner, gh_repo, clone_url)` from a GitHub `(owner, repo)` slug."""
        owner_low = owner.lower()
        repo_low = repo.lower()

        # 1. Exact-named local bare repo pointing at `owner/repo`
        exact_info = self._by_name.get(repo)
        if exact_info is not None and exact_info.remote_for_slug(f"{owner}/{repo}") is not None:
            return repo, owner, repo, exact_info.clone_url or github_clone_url(owner, repo)

        # 2. Plugin-declared known repo matching `(owner, repo)` that is already cloned locally
        matched_known: tuple[str, str, str, str] | None = None
        for known in known_repos.values():
            slug = parse_gh_slug(known.url)
            if slug is not None and (slug[0].lower(), slug[1].lower()) == (owner_low, repo_low):
                matched_known = (known.name, slug[0], slug[1], known.url)
                if known.name in self._by_name:
                    return matched_known
                break

        # 3. Any registered bare repo pointing at `(owner, repo)` (e.g. `odoo/iap-apps` -> `iap`)
        existing = self.find_bare_for_slug(owner_low, repo_low)
        if existing is not None:
            return existing

        # 4. Uncloned plugin-declared known repo matching `(owner, repo)` (e.g. `odoo/odoo` -> `community`)
        if matched_known is not None:
            return matched_known

        return repo, owner, repo, github_clone_url(owner, repo)

    def resolve_identity(
        self,
        owner: str | None,
        repo: str,
        known_repos: Mapping[str, KnownRepo],
        default_org: str,
    ) -> tuple[str, str, str, str]:
        """Resolve `(local_name, gh_owner, gh_repo, clone_url)` from a parsed PR spec."""
        if owner is not None:
            return self.resolve_by_slug(owner, repo, known_repos)

        # 1. Registered local bare repo by exact name (e.g. `iap` -> `iap.git` with origin `odoo/iap-apps`)
        exact_info = self._by_name.get(repo)
        if exact_info is not None and exact_info.slug is not None:
            clone_url = exact_info.clone_url or github_clone_url(*exact_info.slug)
            return repo, exact_info.slug[0], exact_info.slug[1], clone_url

        # 2. Plugin-declared known repo by exact name (reusing an existing bare for the same slug if present)
        known = known_repos.get(repo)
        if known is not None:
            slug = parse_gh_slug(known.url)
            gh_owner, gh_repo = slug if slug is not None else (default_org, repo)
            if known.name in self._by_name:
                return known.name, gh_owner, gh_repo, known.url
            existing = self.find_bare_for_slug(gh_owner.lower(), gh_repo.lower())
            if existing is not None:
                return existing
            return known.name, gh_owner, gh_repo, known.url

        # 3. GitHub slug `<default_org>/<repo>` (matches registered bares or known repos by URL)
        return self.resolve_by_slug(default_org, repo, known_repos)
