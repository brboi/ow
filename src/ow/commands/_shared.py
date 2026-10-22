"""Helpers shared by the ow commands."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from ow import term
from ow.core import git, refresh
from ow.core.paths import repos_dir
from ow.core.plan import Batch, GitStep, RepoPlan, run_batch
from ow.core.session import Session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def refresh_workspace(session: Session) -> int:
    """Refresh the session's workspace and print the summary; 1 when an error diagnostic blocked it."""
    assert session.workspace is not None
    projection, written = refresh.refresh(session.workspace, session.plugins)
    refresh.report(projection, written)
    return 1 if projection.blocked else 0


def refresh_after(session: Session) -> int:
    """The one refresh after a git batch: git just moved branches, so re-derive first."""
    return refresh_workspace(session.reload())


def known_repos(plugins: "Plugins") -> dict[str, str]:
    """Registered repo name -> URL, from every `ow_known_repos` answer."""
    urls: dict[str, str] = {}
    for _plugin, answer in plugins.call("ow_known_repos"):
        for repo in answer or ():
            urls[repo.name] = repo.url
    return urls


def remotes_of(repo_name: str) -> tuple[str, ...]:
    """A bare repo's remote names; `("origin",)` when the bare is missing."""
    bare = (repos_dir() / f"{repo_name}.git").resolve()
    return git.remotes(bare) or ("origin",)


def render_value(value: object) -> str:
    """A raw config value in TOML spelling, as a dropped-key line prints it."""
    if value is None:
        return "(unset)"
    if isinstance(value, list):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {render_value(item)}" for key, item in value.items()) + "}"
    return tomli_w.dumps({"v": value}).split("=", 1)[1].strip()


def print_dropped(dropped: Mapping[str, object]) -> None:
    """Print every key a config rewrite dropped, with its old value."""
    for key, value in dropped.items():
        term.warn(f"dropped {key} = {render_value(value)}")


def run_steps(name: str, steps: Sequence[GitStep], root: Path | None = None) -> int:
    """Print each step as it runs, one after the other; 1 when one failed."""
    outcome = run_batch(
        Batch(root=root, repos=(RepoPlan(repo=name, steps=tuple(steps)),)),
        prompt=False,
        dry_run=False,
        yes=True,
    )
    return outcome.exit_code
