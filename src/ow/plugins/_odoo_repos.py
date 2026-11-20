"""Odoo repository and branch identity shared by the odoo and runbot plugins.

Support module, not a plugin. `ODOO_REPOS` is the single table tying a local repo name to its
GitHub slug, its Runbot repo name (Runbot names repos like GitHub does) and its clone URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ow.plugins._github import bare_gh_info, github_clone_url, parse_git_slug, parse_pr_identity

ODOO_ORG = "odoo"
ODOO_SERIES_RE = re.compile(r"^(?:master|main|(?:saas-)?\d+\.\d+)$")


@dataclass(frozen=True)
class OdooRepo:
    """One Odoo repository: default local name and GitHub repo name under `odoo/`."""

    name: str
    repo: str
    pulls_core: bool = True  # an addon repo that needs an Odoo core companion

    @property
    def slug(self) -> str:
        return f"{ODOO_ORG}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return odoo_clone_url(self.repo)


ODOO_CORE = OdooRepo("community", "odoo", pulls_core=False)
ODOO_REPOS = (
    ODOO_CORE,
    OdooRepo("enterprise", "enterprise"),
    OdooRepo("design-themes", "design-themes"),
    OdooRepo("iap-apps", "iap-apps"),
    OdooRepo("upgrade", "upgrade"),
    OdooRepo("upgrade-util", "upgrade-util"),
    OdooRepo("voip", "voip", pulls_core=False),
)

ODOO_BY_NAME = {repo.name: repo for repo in ODOO_REPOS}
# The core is also commonly registered under its GitHub name (`odoo.git`).
ODOO_CORE_NAMES = frozenset({ODOO_CORE.name, ODOO_CORE.repo})
ODOO_NAMES = frozenset(ODOO_BY_NAME) | ODOO_CORE_NAMES
ODOO_ADDON_NAMES = frozenset(repo.name for repo in ODOO_REPOS if repo.pulls_core)
ODOO_SLUGS = frozenset(repo.slug for repo in ODOO_REPOS)
ODOO_CORE_SLUGS = frozenset({ODOO_CORE.slug})
ODOO_ADDON_SLUGS = frozenset(repo.slug for repo in ODOO_REPOS if repo.pulls_core)


def odoo_clone_url(repo: str) -> str:
    """The clone URL of `odoo/<repo>`."""
    return github_clone_url(ODOO_ORG, repo)


def runbot_repo_name(local_name: str) -> str:
    """Runbot's repo name for a local repo name (e.g. `community` -> `odoo`)."""
    known = ODOO_BY_NAME.get(local_name)
    return known.repo if known is not None else local_name.lower()


def is_odoo_series(branch: str) -> bool:
    """Whether `branch` is an Odoo release/series branch name (`master`, `main`, `18.0`, `saas-*`)."""
    return bool(ODOO_SERIES_RE.fullmatch(branch))


def is_odoo_slug(slug: str) -> bool:
    """Whether `owner/repo` is a canonical Odoo GitHub repository slug."""
    return slug.lower() in ODOO_SLUGS


def is_odoo_repo(
    name: str,
    *,
    url: str | None = None,
    pr: str | None = None,
    bare: Path | None = None,
) -> bool:
    """Whether a repo identified by name, URL, PR identity, or local `bare` is an Odoo repository."""
    if name in ODOO_NAMES:
        return True
    if pr is not None and (parsed_pr := parse_pr_identity(pr)) is not None:
        if is_odoo_slug(parsed_pr[0]):
            return True
    parsed = parse_git_slug(url)
    if parsed is not None and is_odoo_slug(f"{parsed[0]}/{parsed[1]}"):
        return True
    if bare is not None:
        info = bare_gh_info(bare)
        if info is not None and info.gh_slug and is_odoo_slug(info.gh_slug):
            return True
    return False
