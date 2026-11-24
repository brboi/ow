"""Odoo settings: declarations, defaults and validated reads."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from ow.core import config
from ow.core.plugins import Diagnostic, Setting
from ow.plugins.odoo.release import get_core, read_demo_capability
from ow.specs.services import DatabaseService, SmtpService

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

_T = TypeVar("_T")

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
            unique=True,
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
                return default, Diagnostic(level="warning", text=f"{key} must be 1-65535, using default")
            return port, None
        return default, None

    if key in _LIST_KEYS:
        if val is None:
            return default, None
        cleaned = _clean_args(val)
        if cleaned is None:
            return default, Diagnostic(level="warning", text=f"{key} contains invalid item, using default")
        return cleaned, None

    if not isinstance(val, str):
        return default, None
    if "\n" in val or "\r" in val or "\x00" in val or (key in _PLAIN_KEYS and not val):
        return default, Diagnostic(level="warning", text=f"{key} invalid, using default")
    return val, None


@dataclass(frozen=True)
class DatabaseTarget:
    """The PostgreSQL server Odoo connects to, as the generated `odoorc` states it."""

    host: str
    port: int
    user: str
    password: str
    service: DatabaseService | None = None

    @property
    def managed_drop(self) -> Callable[[str], None] | None:
        """The service's drop function when this is the server the service manages, else None."""
        svc = self.service
        if svc is not None and (self.host, self.port) == (svc.host, svc.port):
            return svc.drop
        return None


def _first(answers: Callable[[str], list] | None, hook: str, kind: type[_T]) -> _T | None:
    """The first `kind` answer to `hook`, or None."""
    if answers is None:
        return None
    return next((a for a in answers(hook) if isinstance(a, kind)), None)


def database_target(ws: Workspace, answers: Callable[[str], list] | None) -> DatabaseTarget:
    """The database connection of `ws`: a running database service fills the values the user
    left at their default; a value set in any config layer wins, even when it equals the default."""
    host, _ = get_setting("odoo.db_host", ws.settings, ws)
    port, _ = get_setting("odoo.db_port", ws.settings, ws)
    user, _ = get_setting("odoo.db_user", ws.settings, ws)
    password, _ = get_setting("odoo.db_password", ws.settings, ws)
    svc = _first(answers, "database_service", DatabaseService)
    if svc is not None:
        if not ws.is_explicit("odoo.db_host"):
            host = svc.host
        if not ws.is_explicit("odoo.db_port"):
            port = svc.port
        if not ws.is_explicit("odoo.db_user"):
            user = svc.user
        if not ws.is_explicit("odoo.db_password"):
            password = svc.password
    return DatabaseTarget(str(host), int(port), str(user), str(password), svc)


def smtp_target(ws: Workspace, answers: Callable[[str], list] | None) -> tuple[str, int]:
    """The SMTP `(server, port)` of `ws`, resolved like `database_target`."""
    server, _ = get_setting("odoo.smtp_server", ws.settings, ws)
    port, _ = get_setting("odoo.smtp_port", ws.settings, ws)
    svc = _first(answers, "smtp_service", SmtpService)
    if svc is not None:
        if not ws.is_explicit("odoo.smtp_server"):
            server = svc.host
        if not ws.is_explicit("odoo.smtp_port"):
            port = svc.port
    return str(server), int(port)


def validate_settings(ws: Workspace) -> list[Diagnostic]:
    """Warn about semantically invalid settings, each with a per-key fallback."""
    diagnostics: list[Diagnostic] = []
    for key in _VALIDATED_KEYS:
        _, diag = get_setting(key, ws.settings, ws)
        if diag is not None:
            diagnostics.append(diag)
    return diagnostics
