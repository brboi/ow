"""Tool-agnostic Python environment specification and merging.

Plugins like `odoo` emit a `PythonEnvironment`; environment backends (`mise`,
or a future `uv` plugin) consume it to configure the Python interpreter, shared
or local virtualenv, environment variables, PATH entries, aliases and tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from ow.core.paths import venvs_dir
from ow.core.plugins import Diagnostic, hookspec

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@dataclass(frozen=True)
class PythonEnvironment:
    """Tool-agnostic Python environment contribution."""

    env: Mapping[str, str] = field(default_factory=dict)
    path: tuple[str, ...] = ()
    python: tuple[tuple[int, int], tuple[int, int]] | None = None  # supported (min, max)
    venv_key: str | None = None  # e.g. "18.0", "saas-18.3" for shared venv key
    postinstall: tuple[str, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    tasks: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


class PythonEnvSpec:
    """Hookspec for Python environment contributions."""

    @hookspec
    def python_environment(self, ws: Workspace) -> PythonEnvironment | None:
        """Return a PythonEnvironment contribution or None."""


def merge(answers: Sequence[PythonEnvironment]) -> PythonEnvironment | None:
    """Merge PythonEnvironment answers in tuple order.

    - path and postinstall are concatenated
    - env, aliases, and tasks: later answer wins per key
    - python and venv_key: later non-None wins
    - Returns None when there are no answers
    """
    if not answers:
        return None

    merged_env: dict[str, str] = {}
    merged_path: list[str] = []
    merged_postinstall: list[str] = []
    merged_aliases: dict[str, str] = {}
    merged_tasks: dict[str, Mapping[str, object]] = {}
    merged_python: tuple[tuple[int, int], tuple[int, int]] | None = None
    merged_venv_key: str | None = None

    for answer in answers:
        if answer.path:
            merged_path.extend(answer.path)
        if answer.postinstall:
            merged_postinstall.extend(answer.postinstall)
        if answer.env:
            merged_env.update(answer.env)
        if answer.aliases:
            merged_aliases.update(answer.aliases)
        if answer.tasks:
            merged_tasks.update(answer.tasks)
        if answer.python is not None:
            merged_python = answer.python
        if answer.venv_key is not None:
            merged_venv_key = answer.venv_key

    return PythonEnvironment(
        env=merged_env,
        path=tuple(merged_path),
        python=merged_python,
        venv_key=merged_venv_key,
        postinstall=tuple(merged_postinstall),
        aliases=merged_aliases,
        tasks=merged_tasks,
    )


def select_python_version(
    bounds: tuple[tuple[int, int], tuple[int, int]] | None,
    override: object,
    *,
    setting_name: str = "mise.python",
) -> tuple[str | None, list[Diagnostic]]:
    """Resolve the effective Python `3.MINOR` string from supported bounds and an optional override."""
    diagnostics: list[Diagnostic] = []
    python_value = override if isinstance(override, str) and override else None

    if bounds is None:
        return python_value, diagnostics

    python_min, python_max = bounds
    if python_value is None:
        return f"{python_max[0]}.{python_max[1]}", diagnostics

    match = re.match(r"^3\.(\d+)$", python_value)
    if not match:
        diagnostics.append(
            Diagnostic(
                "warning",
                f"{setting_name} {python_value!r} malformed (expected 3.MINOR), using range max",
            )
        )
        return f"{python_max[0]}.{python_max[1]}", diagnostics

    minor = int(match.group(1))
    min_minor = python_min[1]
    max_minor = python_max[1]
    if minor < min_minor or minor > max_minor:
        clamped_minor = max(min_minor, min(minor, max_minor))
        diagnostics.append(
            Diagnostic(
                "info",
                f"{setting_name} {python_value} clamped to range {python_min[0]}.{min_minor}-{max_minor}",
            )
        )
        return f"{python_max[0]}.{clamped_minor}", diagnostics

    return python_value, diagnostics


def resolve_venv_path(
    ws: Workspace,
    python_version: str | None,
    venv_key: str | None,
    *,
    shared: bool = True,
) -> Path:
    """Return the virtualenv path for `ws`: shared under `$XDG_DATA_HOME/ow/venvs/py<ver>-<key>`
    when `shared` is True and both `python_version` and `venv_key` are known, else `ws.root / ".venv"`.
    """
    if shared and python_version and venv_key:
        safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", venv_key).strip("-")
        if safe_key:
            return venvs_dir() / f"py{python_version}-{safe_key}"
    return ws.root / ".venv"
