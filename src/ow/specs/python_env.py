"""Python environment specification, merging and workspace virtualenv resolution.

Plugins like `odoo` emit a `PythonEnvironment` with workspace-root-relative paths. ow owns
the `python.version` / `python.shared_venv` settings and the venv resolution; an environment
backend turns the result into an interpreter and a virtualenv through the
`python_bootstrap` and `python_wrap` hooks.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core.fs import sidecar_lock
from ow.core.paths import venvs_dir
from ow.core.plugins import Answers, Diagnostic, Setting, hookspec
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

_REQ_STAMP = ".ow-requirements.sha256"

SETTINGS = (
    Setting(
        key="python.version",
        type=str,
        default=None,
        help="Python version of the workspace virtualenv (e.g. '3.11', '3.12'); defaults to the newest the checkout supports",
    ),
    Setting(
        key="python.shared_venv",
        type=bool,
        default=True,
        help="Share Python virtualenvs by version and series under $XDG_DATA_HOME/ow/venvs",
    ),
)


@dataclass(frozen=True, kw_only=True)
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
    """Hookspecs for Python environments: what a workspace needs, and who builds it."""

    @hookspec
    def python_environment(self, ws: Workspace) -> PythonEnvironment | None:
        """Return a PythonEnvironment contribution or None."""

    @hookspec
    def python_bootstrap(self, ws: Workspace, venv: WorkspaceVenv, log_to_stderr: bool) -> bool | None:
        """Install the interpreter and create the virtualenv `venv.path` when it is missing.

        Return True when the plugin took care of it, None to pass. ow checks afterwards that the
        virtualenv exists, so a plugin that could not build it just returns. With `log_to_stderr`
        the plugin keeps stdout clean (`ow exec` feeds pipelines): use `run_bootstrap`.
        """

    @hookspec
    def python_wrap(self, ws: Workspace, argv: tuple[str, ...]) -> tuple[str, ...] | None:
        """Return `argv` prefixed to run inside the environment this plugin manages, or None.

        Used where ow finds no virtualenv to run in (`ow exec` in a workspace without a Python
        environment, a runner without its venv).
        """


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
            Diagnostic(level="warning", text=f"{setting_name} {python_value!r} malformed (expected 3.MINOR), using range max")
        )
        return f"{python_max[0]}.{python_max[1]}", diagnostics

    requested = (int(match.group(1)), int(match.group(2)))
    if requested < python_min or requested > python_max:
        clamped = max(python_min, min(requested, python_max))
        diagnostics.append(
            Diagnostic(level="info", text=f"{setting_name} {python_value} clamped to range "
            f"{python_min[0]}.{python_min[1]}-{python_max[1]}")
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


@dataclass(frozen=True, kw_only=True)
class WorkspaceVenv:
    """Resolved Python version and virtualenv location of a workspace."""

    python_version: str | None
    path: Path
    diagnostics: tuple[Diagnostic, ...] = ()

    def is_local(self, ws: Workspace) -> bool:
        """Whether the virtualenv is `<workspace>/.venv` rather than a shared one."""
        return self.path == ws.root / ".venv"


def workspace_venv(
    ws: Workspace, env: PythonEnvironment | None, *, shareable: bool = True
) -> WorkspaceVenv:
    """Resolve the Python version and virtualenv of `ws` from the merged `env`.

    Settings: `python.version` overrides the version within `env.python`; the venv is
    shared (keyed by `env.venv_key`) when `python.shared_venv` holds and `shareable` is true,
    else it is `<workspace>/.venv`. `shareable` says that something builds the shared venv.
    """
    bounds = env.python if env is not None else None
    python_version, diagnostics = select_python_version(
        bounds, ws.settings.get("python.version"), setting_name="python.version"
    )
    shared = shareable and bool(ws.settings.get("python.shared_venv", True))
    venv_key = env.venv_key if env is not None else None
    path = resolve_venv_path(ws, python_version, venv_key, shared=shared)
    return WorkspaceVenv(python_version=python_version, path=path, diagnostics=tuple(diagnostics))


def resolve_workspace_python(
    ws: Workspace, answers: Answers
) -> tuple[PythonEnvironment | None, WorkspaceVenv] | None:
    """The one place a workspace's Python environment and virtualenv are computed.

    Merges every enabled plugin's `python_environment` answer (`answers` is what ow hands to
    hooks) and resolves the venv from the merged result, so the generated files, the runner,
    `ow exec` and the environment backend can never disagree. The venv is shared only when
    an enabled plugin implements `python_bootstrap` (nobody else would ever create it);
    otherwise it is the workspace's own `.venv`. Returns None when no plugin answers and
    `python.version` is unset (the workspace has no Python environment).
    """
    merged = merge(answers("python_environment"))
    if merged is None and not ws.settings.get("python.version"):
        return None
    return merged, workspace_venv(ws, merged, shareable=bool(answers.implementers("python_bootstrap")))


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


def run_bootstrap(
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


def progress(message: str, *, log_to_stderr: bool) -> None:
    """Print a bootstrap progress line, on stderr when `log_to_stderr` keeps stdout clean."""
    if log_to_stderr:
        print(message, file=sys.stderr)
    else:
        term.out(message)


def wrap_command(
    answers: Callable[..., list], argv: Sequence[str]
) -> tuple[str, ...] | None:
    """`argv` as the first plugin answering `python_wrap` runs it, or None when none does."""
    for wrapped in answers("python_wrap", argv=tuple(argv)):
        return tuple(wrapped)
    return None


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
        ensure = run_bootstrap(
            [str(venv_python), "-m", "ensurepip", "--default-pip"],
            cwd=cwd,
            log_to_stderr=log_to_stderr,
        )
        if ensure.returncode != 0:
            raise UsageError(f"failed to bootstrap pip in {venv_path}")


def ensure_venv_ready(
    ws: Workspace,
    venv: WorkspaceVenv,
    answers: Callable[..., list],
    *,
    requirements: Sequence[str] = (),
    log_to_stderr: bool = False,
) -> None:
    """Ensure the virtualenv `venv` exists and its `requirements` are installed.

    A missing virtualenv is built by whichever plugin answers `python_bootstrap`; with none, it
    stays missing. When `log_to_stderr` is True, progress lines and bootstrap subprocess stdout
    are routed to `stderr` so callers like `ow exec` keep `stdout` clean for pipelines.
    """
    venv_path = venv.path
    venv_python = venv_path / "bin" / "python"
    req_files = tuple(p for rel in requirements if (p := ws.root / rel).is_file())
    req_hash = _requirements_hash(req_files) if req_files else None

    if venv_python.exists():
        # Skip synthetic non-venv interpreters (e.g. test stubs without pyvenv.cfg).
        if not (venv_path / "pyvenv.cfg").is_file():
            return
        if req_hash is None or _is_stamped(venv_path, req_hash):
            return

    with sidecar_lock(venv_path):
        created = False
        if venv_python.exists():
            if not (venv_path / "pyvenv.cfg").is_file():
                return
            if req_hash is None or _is_stamped(venv_path, req_hash):
                return
        else:
            answers("python_bootstrap", venv=venv, log_to_stderr=log_to_stderr)
            if not venv_python.exists() or not (venv_path / "pyvenv.cfg").is_file():
                return
            created = True

        if req_files and req_hash is not None:
            progress(f"# {ws.name}: installing Python requirements into {venv_path}", log_to_stderr=log_to_stderr)
            _ensure_pip(venv_path, venv_python, ws.root, log_to_stderr=log_to_stderr)

            pip_cmd = [str(venv_python), "-m", "pip", "install"]
            for req in req_files:
                pip_cmd.extend(["-r", str(req)])
            res = run_bootstrap(pip_cmd, cwd=ws.root, log_to_stderr=log_to_stderr)
            if res.returncode != 0:
                raise UsageError(f"failed to install Python requirements into {venv_path}")

            (venv_path / _REQ_STAMP).write_text(f"{req_hash}\n", encoding="utf-8")
        elif created:
            _ensure_pip(venv_path, venv_python, ws.root, log_to_stderr=log_to_stderr)
