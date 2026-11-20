"""Odoo settings: declarations, defaults and validated reads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ow.core import config
from ow.core.plugins import Diagnostic, Setting
from ow.plugins.odoo.release import get_core, read_demo_capability

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

_PORT_KEYS = ("odoo.http_port", "odoo.gevent_port", "odoo.db_port", "odoo.smtp_port")
_PLAIN_KEYS = ("odoo.db_name", "odoo.db_host", "odoo.db_user", "odoo.smtp_server")
_PASSWORD_KEYS = ("odoo.db_password", "odoo.admin_passwd")
_LIST_KEYS = ("odoo.debug_args", "odoo.debug_test_args")
_VALIDATED_KEYS = _PORT_KEYS + _PLAIN_KEYS + _PASSWORD_KEYS + _LIST_KEYS

_SETTING_DEFAULTS: dict[str, object] = {}


def _default_debug_args(ws: Workspace) -> list[str]:
    """`--dev=all`, plus `--without-demo=all` for a pre-19 core.

    Old cores spell the flag `--without-demo`; without it a freshly created
    database would load demo data.
    """
    args = ["--dev=all"]
    core, _ = get_core(ws)
    if core is not None and read_demo_capability(core.path) is False:
        args.append("--without-demo=all")
    return args


def declarations() -> list[Setting]:
    """The settings the Odoo plugin declares."""
    return [
        Setting(
            key="odoo.http_port",
            type=int,
            default=8069,
            help="HTTP port for Odoo",
        ),
        Setting(
            key="odoo.gevent_port",
            type=int,
            default=8072,
            help="Gevent/longpolling port for Odoo",
        ),
        Setting(
            key="odoo.db_name",
            type=str,
            default=lambda ws: ws.name,
            help="Database name (defaults to workspace name)",
        ),
        Setting(
            key="odoo.db_host",
            type=str,
            default="localhost",
            help="Database host",
        ),
        Setting(
            key="odoo.db_port",
            type=int,
            default=5432,
            help="Database port",
        ),
        Setting(
            key="odoo.db_user",
            type=str,
            default="odoo",
            help="Database user",
        ),
        Setting(
            key="odoo.db_password",
            type=str,
            default="odoo",
            help="Database password",
        ),
        Setting(
            key="odoo.admin_passwd",
            type=str,
            default="Password",
            help="Odoo admin password",
        ),
        Setting(
            key="odoo.smtp_server",
            type=str,
            default="localhost",
            help="SMTP server",
        ),
        Setting(
            key="odoo.smtp_port",
            type=int,
            default=25,
            help="SMTP port",
        ),
        Setting(
            key="odoo.debug_args",
            type=list,
            default=_default_debug_args,
            help='Arguments of the editor "Run Instance With Debug" launch',
        ),
        Setting(
            key="odoo.debug_test_args",
            type=list,
            default=lambda ws: [f"--test-tags={ws.name}"],
            help='Arguments of the editor "Debug Tests" launch',
        ),
        Setting(
            key="odoo.default_repos",
            type=list,
            default=["community", "enterprise"],
            help="Default repos expanded by `@<base>` specs in `ow init` / `ow add` / `ow run --tmp`",
        ),
    ]


def _setting_default(key: str, ws: Workspace) -> object:
    """The `Setting` table's default for `key`, resolving a per-workspace callable with `ws`."""
    if not _SETTING_DEFAULTS:
        for setting in declarations():
            _SETTING_DEFAULTS[setting.key] = setting.default
    default = _SETTING_DEFAULTS.get(key)
    return default(ws) if callable(default) else default


def _clean_args(value: object) -> list[str] | None:
    """A usable argument list, or None when a string arg could break `argv`."""
    if not isinstance(value, list):
        return None
    if any(
        not isinstance(item, str) or "\n" in item or "\r" in item or "\x00" in item
        for item in value
    ):
        return None
    return list(value)


def get_setting(
    key: str, settings: Mapping[str, object], ws: Workspace
) -> tuple[object, Diagnostic | None]:
    """Validated setting value; an unusable one falls back to its `Setting` default with a warning."""
    val = settings.get(key)
    default = _setting_default(key, ws)

    if key in _PORT_KEYS:
        port = config.as_int(val)
        if port is not None:
            if not 1 <= port <= 65535:
                return default, Diagnostic("warning", f"{key} must be 1-65535, using default")
            return port, None
        return default, None

    if key in _LIST_KEYS:
        if val is None:
            return default, None
        cleaned = _clean_args(val)
        if cleaned is None:
            return default, Diagnostic("warning", f"{key} contains invalid item, using default")
        return cleaned, None

    if not isinstance(val, str):
        return default, None
    if "\n" in val or "\r" in val or "\x00" in val or (key in _PLAIN_KEYS and not val):
        return default, Diagnostic("warning", f"{key} invalid, using default")
    return val, None


def validate_settings(ws: Workspace) -> list[Diagnostic]:
    """Warn about semantically invalid settings, each with a per-key fallback."""
    diagnostics: list[Diagnostic] = []
    for key in _VALIDATED_KEYS:
        _, diag = get_setting(key, ws.settings, ws)
        if diag is not None:
            diagnostics.append(diag)
    return diagnostics
