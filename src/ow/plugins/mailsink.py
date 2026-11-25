"""Mailsink plugin: global shared Mailpit container auto-started on `ow run`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ow import term
from ow.core import config
from ow.plugin_api import RepoFacts, Setting, SmtpService, StatusInfo, Workspace, hookimpl
from ow.plugins import _containers as containers

_CONTAINER = "ow-mailsink"

_SETTINGS = (
    Setting(key="mailsink.image", type=str, default="axllent/mailpit:v1.34.0", help="Mailpit container image"),
    Setting(key="mailsink.smtp_port", type=int, default=1025, help="Host SMTP port for mailsink"),
    Setting(key="mailsink.web_port", type=int, default=8025, help="Host Web UI port for mailsink"),
)
_DEFAULTS = {setting.key: setting.default for setting in _SETTINGS}


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide mailsink (Mailpit) settings."""
    return list(_SETTINGS)


def _port(settings: Mapping[str, object], key: str) -> int:
    """The port setting `key`, or its declared default when it is not a valid port."""
    raw = config.as_int(settings.get(key))
    return raw if raw is not None and 1 <= raw <= 65535 else int(_DEFAULTS[key])


def _cfg(settings: Mapping[str, object]) -> tuple[str, int, int]:
    image = str(settings.get("mailsink.image") or _DEFAULTS["mailsink.image"])
    return image, _port(settings, "mailsink.smtp_port"), _port(settings, "mailsink.web_port")


@hookimpl
def smtp_service(ws: Workspace) -> SmtpService | None:
    """Expose Mailpit SMTP details when a container engine is ready."""
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        return None
    _image, smtp_port, web_port = _cfg(ws.settings)
    return SmtpService(
        host="localhost",
        port=smtp_port,
        web_url=f"http://localhost:{web_port}",
    )


@hookimpl
def ow_before_run(ws: Workspace, workspaces: Sequence[Workspace]) -> list[str] | None:
    """Ensure the shared Mailpit container is running before `ow run`."""
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        term.warn("mailsink: docker/podman unavailable; skipping container startup")
        return None

    image, smtp_port, web_port = _cfg(ws.settings)
    try:
        state = containers.inspect_container(engine, _CONTAINER)
        if state is None or state.status != "running":
            term.out(f"# mailsink: starting {_CONTAINER} ({image})...")
        containers.ensure_container(
            engine,
            _CONTAINER,
            image=image,
            ports=((smtp_port, 1025), (web_port, 8025)),
        )
    except RuntimeError as exc:
        term.warn(f"mailsink: {exc}")
        return None

    return [f"mailsink: smtp://localhost:{smtp_port} (ui: http://localhost:{web_port})"]


@hookimpl
def ow_stop_services(ws: Workspace | None) -> list[str] | None:
    """Stop the shared Mailpit container (`ow stop --services` / `--all`)."""
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        return None
    try:
        if containers.stop_container(engine, _CONTAINER):
            return [f"mailsink: stopped {_CONTAINER}"]
    except RuntimeError as exc:
        term.warn(f"mailsink: {exc}")
    return None


@hookimpl
def ow_status(
    ws: Workspace | None,
    facts: Sequence[RepoFacts],
    spec: str | None,
    offline: bool,
    settings: Mapping[str, object],
) -> list[StatusInfo]:
    """Report shared Mailpit container status in `ow status`."""
    if spec is not None or ws is None:
        return []
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        return []

    state = containers.inspect_container(engine, _CONTAINER)
    if state is None:
        return []

    _image, smtp_port, web_port = _cfg(ws.settings)
    effective_smtp = state.ports.get(1025, smtp_port)
    effective_web = state.ports.get(8025, web_port)
    url = f"http://localhost:{effective_web}"
    return [
        StatusInfo(
            plugin="mailsink",
            scope="service",
            subject="mailsink",
            summary=f"{state.status} on smtp://localhost:{effective_smtp} (ui: {url})",
        )
    ]
