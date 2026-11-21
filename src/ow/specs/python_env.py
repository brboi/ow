"""Python environment specification, merging and workspace virtualenv resolution.

Plugins like `odoo` emit a `PythonEnvironment` with workspace-root-relative
paths; environment backends (`mise`) consume it to configure the interpreter,
virtualenv, environment variables, PATH entries and aliases.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core.fs import sidecar_lock
from ow.core.paths import venvs_dir
from ow.core.plugins import Diagnostic, hookspec
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

_REQ_STAMP = ".ow-requirements.sha256"


@dataclass(frozen=True)
class PythonEnvironment:
    """Python environment contribution; every path is relative to the workspace root."""

    env: Mapping[str, str] = field(default_factory=dict)
    env_paths: Mapping[str, str] = field(default_factory=dict)  # variable -> path
    path: tuple[str, ...] = ()  # directories added to PATH
    python: tuple[tuple[int, int], tuple[int, int]] | None = None  # supported (min, max)
    venv_key: str | None = None  # e.g. "18.0", "saas-18.3" for shared venv key
    requirements: tuple[str, ...] = ()  # requirements files installed into the venv
    postinstall: tuple[str, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)


class PythonEnvSpec:
    """Hookspec for Python environment contributions."""

    @hookspec
    def python_environment(self, ws: Workspace) -> PythonEnvironment | None:
        """Return a PythonEnvironment contribution or None."""


def merge(answers: Sequence[PythonEnvironment]) -> PythonEnvironment | None:
    """Merge PythonEnvironment answers in tuple order.

    - path, requirements and postinstall are concatenated
    - env, env_paths and aliases: later answer wins per key
    - python and venv_key: later non-None wins
    - Returns None when there are no answers
    """
    if not answers:
        return None

    merged_env: dict[str, str] = {}
    merged_env_paths: dict[str, str] = {}
    merged_path: list[str] = []
    merged_requirements: list[str] = []
    merged_postinstall: list[str] = []
    merged_aliases: dict[str, str] = {}
    merged_python: tuple[tuple[int, int], tuple[int, int]] | None = None
    merged_venv_key: str | None = None

    for answer in answers:
        merged_path.extend(answer.path)
        merged_requirements.extend(answer.requirements)
        merged_postinstall.extend(answer.postinstall)
        merged_env.update(answer.env)
        merged_env_paths.update(answer.env_paths)
        merged_aliases.update(answer.aliases)
        if answer.python is not None:
            merged_python = answer.python
        if answer.venv_key is not None:
            merged_venv_key = answer.venv_key

    return PythonEnvironment(
        env=merged_env,
        env_paths=merged_env_paths,
        path=tuple(merged_path),
        python=merged_python,
        venv_key=merged_venv_key,
        requirements=tuple(merged_requirements),
        postinstall=tuple(merged_postinstall),
        aliases=merged_aliases,
    )


def select_python_version(
    bounds: tuple[tuple[int, int], tuple[int, int]] | None,
    override: object,
    *,
    setting_name: str,
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


@dataclass(frozen=True)
class WorkspaceVenv:
    """Resolved Python version and virtualenv location of a workspace."""

    python_version: str | None
    path: Path
    diagnostics: tuple[Diagnostic, ...] = ()

    def is_local(self, ws: Workspace) -> bool:
        """Whether the virtualenv is `<workspace>/.venv` rather than a shared one."""
        return self.path == ws.root / ".venv"


def workspace_venv(ws: Workspace, env: PythonEnvironment | None) -> WorkspaceVenv:
    """Resolve the Python version and virtualenv of `ws` from the merged `env`.

    Settings: `mise.python` overrides the version within `env.python`; the venv is
    shared (keyed by `env.venv_key`) when `mise.enabled` and `mise.shared_venv` hold.
    """
    bounds = env.python if env is not None else None
    python_version, diagnostics = select_python_version(
        bounds, ws.settings.get("mise.python"), setting_name="mise.python"
    )
    shared = bool(ws.settings.get("mise.enabled", True)) and bool(
        ws.settings.get("mise.shared_venv", True)
    )
    venv_key = env.venv_key if env is not None else None
    path = resolve_venv_path(ws, python_version, venv_key, shared=shared)
    return WorkspaceVenv(python_version, path, tuple(diagnostics))


def _requirements_hash(files: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_stamped(venv_path: Path, req_hash: str) -> bool:
    stamp = venv_path / _REQ_STAMP
    if not stamp.is_file():
        return False
    try:
        return stamp.read_text(encoding="utf-8").strip() == req_hash
    except OSError:
        return False


def _run_bootstrap(
    cmd: Sequence[str],
    *,
    cwd: Path,
    log_to_stderr: bool,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a venv bootstrap command, routing its stdout to stderr when `log_to_stderr` is True."""
    if not log_to_stderr:
        return subprocess.run(cmd, cwd=cwd, check=False)
    try:
        sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if res.stdout:
            sys.stderr.write(res.stdout)
        if res.stderr:
            sys.stderr.write(res.stderr)
        return res
    return subprocess.run(cmd, cwd=cwd, stdout=sys.stderr, check=False)


def _ensure_pip(
    venv_path: Path, venv_python: Path, cwd: Path, *, log_to_stderr: bool = False
) -> None:
    """Bootstrap `pip` via `ensurepip` when missing from `venv_path`."""
    has_pip = subprocess.run(
        [str(venv_python), "-m", "pip", "--version"],
        capture_output=True,
        check=False,
    )
    if has_pip.returncode != 0:
        ensure = _run_bootstrap(
            [str(venv_python), "-m", "ensurepip", "--default-pip"],
            cwd=cwd,
            log_to_stderr=log_to_stderr,
        )
        if ensure.returncode != 0:
            raise UsageError(f"failed to bootstrap pip in {venv_path}")


def ensure_venv_ready(
    ws: Workspace,
    venv_path: Path,
    *,
    requirements: Sequence[str] = (),
    log_to_stderr: bool = False,
) -> None:
    """Ensure `venv_path` exists (via `mise` when enabled) and its `requirements` are installed.

    When `log_to_stderr` is True, progress lines and bootstrap subprocess stdout are routed
    to `stderr` so callers like `ow exec` keep `stdout` clean for pipelines.
    """
    venv_python = venv_path / "bin" / "python"
    req_files = tuple(p for rel in requirements if (p := ws.root / rel).is_file())
    req_hash = _requirements_hash(req_files) if req_files else None

    if venv_python.exists():
        # Skip synthetic non-venv interpreters (e.g. test stubs without pyvenv.cfg).
        if not (venv_path / "pyvenv.cfg").is_file():
            return
        if req_hash is None or _is_stamped(venv_path, req_hash):
            return

    def _log(msg: str) -> None:
        if log_to_stderr:
            print(msg, file=sys.stderr)
        else:
            term.out(msg)

    with sidecar_lock(venv_path):
        created = False
        if venv_python.exists():
            if not (venv_path / "pyvenv.cfg").is_file():
                return
            if req_hash is None or _is_stamped(venv_path, req_hash):
                return
        else:
            if not (ws.settings.get("mise.enabled", True) and shutil.which("mise")):
                return
            _log(f"# {ws.name}: preparing Python virtualenv at {venv_path}")
            _run_bootstrap(["mise", "install"], cwd=ws.root, log_to_stderr=log_to_stderr)
            _run_bootstrap(
                ["mise", "exec", "--", "python", "-c", ""],
                cwd=ws.root,
                log_to_stderr=log_to_stderr,
            )
            if not venv_python.exists() or not (venv_path / "pyvenv.cfg").is_file():
                return
            created = True

        if req_files and req_hash is not None:
            _log(f"# {ws.name}: installing Python requirements into {venv_path}")
            _ensure_pip(venv_path, venv_python, ws.root, log_to_stderr=log_to_stderr)

            pip_cmd = [str(venv_python), "-m", "pip", "install"]
            for req in req_files:
                pip_cmd.extend(["-r", str(req)])
            res = _run_bootstrap(pip_cmd, cwd=ws.root, log_to_stderr=log_to_stderr)
            if res.returncode != 0:
                raise UsageError(f"failed to install Python requirements into {venv_path}")

            (venv_path / _REQ_STAMP).write_text(f"{req_hash}\n", encoding="utf-8")
        elif created:
            _ensure_pip(venv_path, venv_python, ws.root, log_to_stderr=log_to_stderr)
