"""Shared local service hookspecs (`DatabaseService`, `SmtpService`).

Service plugins like `postgres` and `mailsink` contribute connection details;
consumer plugins like `odoo` read them through `answers(...)` without knowing
which backend provides them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ow.core.plugins import hookspec

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@dataclass(frozen=True, kw_only=True)
class DatabaseService:
    """Database connection details contributed by a database plugin.

    `user`/`password` is the role applications connect with (not a superuser).
    `admin_user`/`admin_password` is the superuser, for plugins that need to create
    roles or databases; do not assume its password equals `password`.
    Both default to empty when a provider has no superuser to expose.
    `web_url` is the database's web UI (e.g. pgweb), when one runs.
    """

    host: str
    port: int
    user: str
    password: str
    web_url: str | None = None
    drop: Callable[[str], None] | None = None
    admin_user: str = ""
    admin_password: str = ""


@dataclass(frozen=True, kw_only=True)
class SmtpService:
    """SMTP server details contributed by a mail-capture plugin."""

    host: str
    port: int
    web_url: str | None = None


class ServicesSpec:
    """Hookspec for shared local service contributions."""

    @hookspec
    def database_service(self, ws: Workspace) -> DatabaseService | None:
        """Return DatabaseService connection details, or None."""

    @hookspec
    def smtp_service(self, ws: Workspace) -> SmtpService | None:
        """Return SmtpService connection details, or None."""
