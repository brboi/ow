"""Local Odoo run command."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from ow.core.plugins import RunCommand
from ow.errors import UsageError
from ow.plugins.odoo.files import odoo_venv, python_environment
from ow.plugins.odoo.settings import get_setting
from ow.specs.python_env import ensure_venv_ready
from ow.specs.services import DatabaseService

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace


def run_command(ws: Workspace, core: Repo, args: tuple[str, ...]) -> RunCommand:
    """Run Odoo using the workspace's resolved Python virtualenv (or `mise exec`)."""
    py_env = python_environment(core)
    venv_path = odoo_venv(ws, core).path
    ensure_venv_ready(ws, venv_path, requirements=py_env.requirements)
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


_SYSTEM_DBS = frozenset({"postgres", "template0", "template1"})


def _drop_host_database(
    db_name: str, *, host: str, port: int, user: str, password: str
) -> None:
    """Drop `db_name` against a non-container PostgreSQL server via `psql` or `dropdb`."""
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    ident = '"' + db_name.replace('"', '""') + '"'

    psql = shutil.which("psql")
    if psql:
        cmd = [
            psql,
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            "postgres",
            "-c",
            f"DROP DATABASE IF EXISTS {ident} WITH (FORCE);",
        ]
    else:
        dropdb = shutil.which("dropdb")
        if not dropdb:
            raise UsageError(
                f"cannot drop database {db_name!r}: neither managed postgres container nor `psql`/`dropdb` is available"
            )
        cmd = [
            dropdb,
            "--if-exists",
            "--force",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            db_name,
        ]

    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise UsageError(f"failed to drop database {db_name!r}: {detail}")


def reset_workspace(
    ws: Workspace, answers: Callable[[str], list]
) -> list[str]:
    """Drop the workspace's Odoo database and remove its local filestore."""
    db_name_val, _ = get_setting("odoo.db_name", ws.settings, ws)
    db_name = str(db_name_val)
    if not db_name or db_name in _SYSTEM_DBS:
        raise UsageError(f"refusing to drop database {db_name!r} for workspace {ws.name!r}")

    db_host_val, _ = get_setting("odoo.db_host", ws.settings, ws)
    db_port_val, _ = get_setting("odoo.db_port", ws.settings, ws)
    db_user_val, _ = get_setting("odoo.db_user", ws.settings, ws)
    db_password_val, _ = get_setting("odoo.db_password", ws.settings, ws)
    db_host = str(db_host_val)
    db_port = int(db_port_val)
    db_user = str(db_user_val)
    db_password = str(db_password_val)

    db_services = [a for a in answers("database_service") if isinstance(a, DatabaseService)]
    used_managed_drop = False
    if db_services and db_host == "localhost" and db_port == 5432:
        svc = db_services[0]
        if svc.drop is not None:
            svc.drop(db_name)
            used_managed_drop = True

    if not used_managed_drop:
        _drop_host_database(
            db_name,
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_password,
        )

    filestore_root = (ws.root / ".odoo" / "filestore").resolve()
    filestore_dir = (filestore_root / db_name).resolve()
    removed_filestore = False
    if filestore_dir.parent == filestore_root and filestore_dir.exists():
        shutil.rmtree(filestore_dir)
        removed_filestore = True

    if removed_filestore:
        return [f"{ws.name}: dropped database {db_name!r} and filestore"]
    return [f"{ws.name}: dropped database {db_name!r}"]
