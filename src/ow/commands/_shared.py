"""Helpers shared by the ow commands."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from ow import term
from ow.core.git import remotes_of
from ow.core.plan import Batch, GitStep, RepoPlan, Step, run_batch
from ow.core.refresh import refresh_after, refresh_workspace
from ow.errors import PluginError

if TYPE_CHECKING:
    from ow.core.plugins import KnownRepo, Plugins
    from ow.core.workspace import Workspace

__all__ = [
    "known_repo_catalog",
    "prepare_services",
    "print_dropped",
    "refresh_after",
    "refresh_workspace",
    "remotes_of",
    "render_value",
    "run_steps",
]


def prepare_services(workspaces: Sequence["Workspace"], plugins: "Plugins") -> None:
    """Invoke `ow_before_run` once across enabled plugins; failures warn without blocking."""
    if not workspaces:
        return
    results = plugins.call(
        "ow_before_run",
        ws=workspaces[0],
        workspaces=tuple(workspaces),
        errors="collect",
    )
    for plugin_name, result in results:
        if isinstance(result, PluginError):
            term.warn(f"{plugin_name}: {result.cause}")
        elif isinstance(result, list):
            for line in result:
                if isinstance(line, str) and line:
                    term.out(f"# {line}")


def known_repo_catalog(plugins: "Plugins", ws: "Workspace | None" = None) -> dict[str, "KnownRepo"]:
    """Canonical repo name -> KnownRepo from every `ow_known_repos` answer of plugins enabled for `ws`."""
    catalog: dict[str, "KnownRepo"] = {}
    for _plugin, answer in plugins.call("ow_known_repos", ws=ws):
        for repo in answer or ():
            catalog[repo.name] = repo
    return catalog


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


def run_steps(name: str, steps: Sequence[Step], root: Path | None = None) -> int:
    """Print each step as it runs, one after the other; 1 when one failed."""
    outcome = run_batch(
        Batch(root=root, repos=(RepoPlan(repo=name, steps=tuple(steps)),)),
        prompt=False,
        dry_run=False,
        yes=True,
    )
    return outcome.exit_code
