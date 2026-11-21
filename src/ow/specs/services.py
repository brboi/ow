"""Shared local service hookspecs (`DatabaseService`, `SmtpService`).

Service plugins like `postgres` and `mailsink` contribute connection details;
consumer plugins like `odoo` read them through `answers(...)` without knowing
which backend provides them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ow.core.plugins import hookspec

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@dataclass(frozen=True)
class DatabaseService:
    """Database connection details contributed by a database plugin."""

    host: str
    port: int
    user: str
    password: str
    admin_url: str | None = None


@dataclass(frozen=True)
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
