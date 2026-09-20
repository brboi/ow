"""The machine-wide Compose services, generated from one fixed document.

The file is JSON — Compose reads JSON as YAML — so a colon in an absolute
volume source path cannot be misparsed the way a `host:container` shorthand
would be; the long bind syntax carries it as a string. It lives outside any
workspace and outside the workspace lock: ow regenerates it, so users who
need a customized stack keep their own compose file elsewhere. Nothing here
starts a container or upgrades a database.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from ow.utils import paths
from ow.utils.generate import json_text

COMPOSE_NAME = "compose.yml"


def compose_document(volumes_dir: Path) -> dict[str, object]:
    """The fixed service data, with `volumes_dir` as its volume root."""
    return {
        "services": {
            "postgres": {
                "image": "pgvector/pgvector:pg17",
                "environment": {
                    "POSTGRES_USER": "odoo",
                    "POSTGRES_PASSWORD": "odoo",
                    "POSTGRES_DB": "postgres",
                },
                "ports": ["5432:5432"],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(volumes_dir / "postgres-data"),
                        "target": "/var/lib/postgresql/data",
                    }
                ],
                "restart": "unless-stopped",
            },
            "mailpit": {
                "image": "axllent/mailpit",
                "ports": ["8025:8025", "1025:1025"],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(volumes_dir / "mailpit-data"),
                        "target": "/data",
                    }
                ],
                "restart": "unless-stopped",
            },
            "pgweb": {
                "image": "sosedoff/pgweb",
                "ports": ["8081:8081"],
                "environment": {
                    "DATABASE_URL": "postgres://odoo:odoo@postgres:5432/postgres?sslmode=disable"
                },
                "depends_on": ["postgres"],
                "restart": "unless-stopped",
            },
        }
    }


def compose_bytes(volumes_dir: Path) -> bytes:
    """`compose_document` as the file's bytes: ow's JSON formatting, UTF-8."""
    return json_text(compose_document(volumes_dir)).encode("utf-8")


def _unsafe_destination(destination: Path) -> str | None:
    """Why `destination` may not be written, or None when it may.

    Only the part of the path ow owns is inspected: the services directory
    and the file itself. Whether the config home and its own ancestors are
    real directories is the user's setup, not a conflict ow invents.
    """
    for path in (destination.parent, destination):
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"cannot stat {path}: {exc}"
        if stat.S_ISLNK(st.st_mode):
            return f"{path} is a symlink"
        if path == destination.parent:
            if not stat.S_ISDIR(st.st_mode):
                return f"{path} is not a directory"
        elif not stat.S_ISREG(st.st_mode):
            return f"{path} is not a regular file"
    return None


def _atomic_write(destination: Path, data: bytes) -> None:
    """Write `data` through a same-directory temp file and `os.replace`."""
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(data)
        os.replace(tmp_name, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def ensure_services_compose() -> Path:
    """Write the current compose document to `services_dir()/compose.yml`.

    Equal bytes are left alone, mtime included — most renders change
    nothing. A real change lands atomically, so Compose never reads a
    half-written file, and a destination or directory ow cannot safely
    replace is an error naming it rather than a write through the conflict.
    """
    destination = paths.services_dir() / COMPOSE_NAME
    problem = _unsafe_destination(destination)
    if problem is not None:
        raise ValueError(f"services compose: {problem}")

    data = compose_bytes(paths.volumes_dir())
    try:
        current = destination.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise OSError(f"cannot read {destination}: {exc}") from exc
    if current == data:
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(destination, data)
    return destination