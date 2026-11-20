"""Local Odoo run command."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from ow.core.plugins import RunCommand
from ow.plugins.odoo.files import odoo_venv

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace


def run_command(ws: Workspace, core: Repo, args: tuple[str, ...]) -> RunCommand:
    """Run Odoo using the workspace's resolved Python virtualenv (or `mise exec`)."""
    venv_python = odoo_venv(ws, core).path / "bin" / "python"
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
