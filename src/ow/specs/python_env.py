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

    match = re.fullmatch(r"(\d+)\.(\d+)", python_value)
    if not match:
        diagnostics.append(
            Diagnostic(
                "warning",
                f"{setting_name} {python_value!r} malformed (expected 3.MINOR), using range max",
            )
        )
        return f"{python_max[0]}.{python_max[1]}", diagnostics

    requested = (int(match.group(1)), int(match.group(2)))
    if requested < python_min or requested > python_max:
        clamped = max(python_min, min(requested, python_max))
        diagnostics.append(
            Diagnostic(
                "info",
                f"{setting_name} {python_value} clamped to range "
                f"{python_min[0]}.{python_min[1]}-{python_max[1]}",
            )
        )
        return f"{clamped[0]}.{clamped[1]}", diagnostics

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
        safe_ver = re.sub(r"[^A-Za-z0-9._-]+", "-", python_version).strip("-.")
        safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", venv_key).strip("-.")
        if safe_ver and safe_key and ".." not in safe_ver and ".." not in safe_key:
            base = venvs_dir()
            candidate = base / f"py{safe_ver}-{safe_key}"
            if candidate.parent == base:
                return candidate
    return ws.root / ".venv"
