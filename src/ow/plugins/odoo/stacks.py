"""Odoo values of a stack: coordinated ports, pinned database name, stack name and info."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from ow.core import config
from ow.core.stacks import next_free_port
from ow.plugin_api import StackInfo, Workspace
from ow.plugins._odoo_repos import ODOO_CORE_NAMES
from ow.plugins.odoo.release import get_core, is_odoo_core

_PORT_KEYS = ("odoo.http_port", "odoo.gevent_port")


def _has_odoo_core(
    ws: Workspace | None,
    repo_paths: Sequence[Path] | None,
    repo_names: Sequence[str] | None,
) -> bool:
    """Whether the target stack holds (or plans to hold) an Odoo core checkout."""
    if ws is not None:
        repos = getattr(ws, "repos", None)
        if repos:
            return any(is_odoo_core(repo.path) for repo in repos)
        return True
    if repo_paths is not None:
        return any(is_odoo_core(path) for path in repo_paths)
    if repo_names is not None:
        return any(name in ODOO_CORE_NAMES for name in repo_names)
    return True


def stack_values(
    ws: Workspace | None,
    ws_values: Mapping[str, object],
    siblings: Sequence[Workspace],
    pending: Sequence[Mapping[str, object]],
    global_settings: Mapping[str, object],
    *,
    repo_paths: Sequence[Path] | None = None,
    repo_names: Sequence[str] | None = None,
) -> dict[str, object] | None:
    """Pin `odoo.http_port` and `odoo.gevent_port` (non-colliding), and `odoo.db_name` on promotion.

    Stacks without an Odoo core checkout receive nothing. A promoted workspace (`ws`) keeps its
    current database name and ports; a new stack gets the next free ports not used by `siblings`,
    `pending` stacks or its own explicit values, counting up from the resolved global port.
    """
    if ws_values.get("odoo.enabled") is False:
        return None
    if not _has_odoo_core(ws, repo_paths, repo_names):
        return None

    pinned: dict[str, object] = {}
    if ws is not None and "odoo.db_name" not in ws_values:
        effective_db = ws.settings.get("odoo.db_name")
        pinned["odoo.db_name"] = (
            effective_db if isinstance(effective_db, str) and effective_db else ws.name
        )

    used_ports: set[int] = set()
    for settings in (*(s.settings for s in siblings), *pending, ws_values):
        for key in _PORT_KEYS:
            if (port := config.as_int(settings.get(key))) is not None:
                used_ports.add(port)

    for key in _PORT_KEYS:
        if key in ws_values:
            continue
        current = config.as_int(ws.settings.get(key)) if ws is not None else None
        if current is not None:
            port = current
        else:
            port = next_free_port(used_ports, base_port=cast(int, global_settings[key]))
        pinned[key] = port
        used_ports.add(port)
    return pinned or None


def stack_name(ws: Workspace) -> str | None:
    """`odoo` when `ws` contains an Odoo core checkout."""
    if any(is_odoo_core(repo.path) for repo in ws.repos):
        return "odoo"
    return None


def stack_info(ws: Workspace) -> StackInfo | None:
    """The HTTP and gevent ports of `ws` (only when `ws` has an Odoo core checkout)."""
    if ws.repos:
        core, diags = get_core(ws)
        if core is None or diags:
            return None
    ports = {
        label: port
        for label, key in (("http", "odoo.http_port"), ("gevent", "odoo.gevent_port"))
        if (port := config.as_int(ws.settings.get(key))) is not None
    }
    if not ports:
        return None
    return StackInfo(ports=ports, note=f"port {ports['http']}" if "http" in ports else None)
