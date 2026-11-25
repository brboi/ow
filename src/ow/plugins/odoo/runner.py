"""Local Odoo run command."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ow.plugin_api import Repo, RunCommand, UsageError, Workspace
from ow.plugins.odoo.settings import DatabaseTarget, database_target, get_setting
from ow.specs.python_env import ensure_venv_ready, resolve_workspace_python, wrap_command


def run_command(
    ws: Workspace, core: Repo, args: tuple[str, ...], answers: Callable[..., list]
) -> RunCommand:
    """Run Odoo using the workspace's resolved Python virtualenv (else the plugin-managed or system Python)."""
    resolved = resolve_workspace_python(ws, answers)
    assert resolved is not None, "the odoo plugin answers python_environment for an Odoo workspace"
    merged, venv = resolved
    ensure_venv_ready(ws, venv, answers, requirements=merged.requirements if merged else ())
    venv_python = venv.path / "bin" / "python"
    odoorc = ws.root / "odoorc"
    odoo_bin = core.path / "odoo-bin"
    env = {"ODOO_RC": str(odoorc)}
    odoo_args = (str(odoo_bin), "-c", str(odoorc), *args)

    if venv_python.exists():
        argv = (str(venv_python), *odoo_args)
    else:
        argv = wrap_command(answers, ("python", *odoo_args)) or ("python3", *odoo_args)

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


def _reset_target(ws: Workspace, answers: Callable[[str], list]) -> tuple[str, DatabaseTarget, Path | None]:
    """The `(db_name, database, filestore_dir)` that `--reset` removes for `ws`.

    The database is the one the generated `odoorc` points at (`database_target`); the
    filestore is None when there is none to remove.
    """
    db_name_val, _ = get_setting("odoo.db_name", ws.settings, ws)
    db_name = str(db_name_val)
    if not db_name or db_name in _SYSTEM_DBS:
        raise UsageError(f"refusing to drop database {db_name!r} for workspace {ws.name!r}")
    filestore_root = (ws.root / ".odoo" / "filestore").resolve()
    filestore_dir = (filestore_root / db_name).resolve()
    present = filestore_dir.parent == filestore_root and filestore_dir.exists()
    return db_name, database_target(ws, answers), filestore_dir if present else None


def reset_preview(ws: Workspace, answers: Callable[[str], list]) -> list[str]:
    """One line per thing `reset_workspace` would remove: the database, then the filestore if present."""
    db_name, db, filestore_dir = _reset_target(ws, answers)
    lines = [f"database {db.user}@{db.host}:{db.port}/{db_name}"]
    if filestore_dir is not None:
        lines.append(f"filestore {filestore_dir}")
    return lines


def reset_workspace(
    ws: Workspace, answers: Callable[[str], list]
) -> list[str]:
    """Drop the workspace's Odoo database and remove its local filestore."""
    db_name, db, filestore_dir = _reset_target(ws, answers)

    if (drop := db.managed_drop) is not None:
        drop(db_name)
    else:
        _drop_host_database(
            db_name,
            host=db.host,
            port=db.port,
            user=db.user,
            password=db.password,
        )

    if filestore_dir is not None:
        shutil.rmtree(filestore_dir)
        return [f"{ws.name}: dropped database {db_name!r} and filestore"]
    return [f"{ws.name}: dropped database {db_name!r}"]
