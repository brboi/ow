"""Local Odoo run command."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core.fs import sidecar_lock
from ow.core.plugins import RunCommand
from ow.errors import UsageError
from ow.plugins.odoo.files import odoo_venv, python_environment

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace

_REQ_STAMP = ".ow-requirements.sha256"


def _requirements_files(ws: Workspace, core: Repo) -> tuple[Path, ...]:
    env = python_environment(core)
    return tuple(p for rel in env.requirements if (p := ws.root / rel).is_file())


def _requirements_hash(files: tuple[Path, ...]) -> str:
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


def _ensure_venv_ready(ws: Workspace, core: Repo, venv_path: Path) -> None:
    """Ensure the virtualenv exists (via mise) and its requirements are installed."""
    venv_python = venv_path / "bin" / "python"
    req_files = _requirements_files(ws, core)
    if not req_files:
        return

    req_hash = _requirements_hash(req_files)

    if venv_python.exists() and _is_stamped(venv_path, req_hash):
        return

    # Skip synthetic non-venv interpreters (e.g. test stubs without pyvenv.cfg).
    if venv_python.exists() and not (venv_path / "pyvenv.cfg").is_file():
        return

    with sidecar_lock(venv_path):
        if venv_python.exists() and _is_stamped(venv_path, req_hash):
            return

        if not venv_python.exists():
            if not (ws.settings.get("mise.enabled", True) and shutil.which("mise")):
                return
            term.out(f"# {ws.name}: preparing Python virtualenv at {venv_path}")
            subprocess.run(["mise", "install"], cwd=ws.root, check=False)
            subprocess.run(["mise", "exec", "--", "python", "-c", ""], cwd=ws.root, check=False)
            if not venv_python.exists() or not (venv_path / "pyvenv.cfg").is_file():
                return

        term.out(f"# {ws.name}: installing Python requirements into {venv_path}")
        has_pip = subprocess.run(
            [str(venv_python), "-m", "pip", "--version"],
            capture_output=True,
            check=False,
        )
        if has_pip.returncode != 0:
            ensure = subprocess.run(
                [str(venv_python), "-m", "ensurepip", "--default-pip"],
                cwd=ws.root,
                check=False,
            )
            if ensure.returncode != 0:
                raise UsageError(f"failed to bootstrap pip in {venv_path}")

        pip_cmd = [str(venv_python), "-m", "pip", "install"]
        for req in req_files:
            pip_cmd.extend(["-r", str(req)])
        res = subprocess.run(pip_cmd, cwd=ws.root, check=False)
        if res.returncode != 0:
            raise UsageError(f"failed to install Python requirements into {venv_path}")

        (venv_path / _REQ_STAMP).write_text(f"{req_hash}\n", encoding="utf-8")


def run_command(ws: Workspace, core: Repo, args: tuple[str, ...]) -> RunCommand:
    """Run Odoo using the workspace's resolved Python virtualenv (or `mise exec`)."""
    venv_path = odoo_venv(ws, core).path
    _ensure_venv_ready(ws, core, venv_path)
    venv_python = venv_path / "bin" / "python"
    odoorc = ws.root / "odoorc"
    odoo_bin = core.path / "odoo-bin"
    env = {"ODOO_RC": str(odoorc)}

    if venv_python.exists():
        argv = (str(venv_python), str(odoo_bin), "-c", str(odoorc), *args)
    elif ws.settings.get("mise.enabled", True) and shutil.which("mise"):
        argv = ("mise", "exec", "--", "python", str(odoo_bin), "-c", str(odoorc), *args)
    else:
        argv = ("python3", str(odoo_bin), "-c", str(odoorc), *args)

    return RunCommand(argv=argv, cwd=ws.root, env=env)
