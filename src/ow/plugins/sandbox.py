"""Sandbox plugin: declares settings for `ow sandbox`."""

from __future__ import annotations

from ow.plugin_api import Setting, hookimpl


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide settings configuring `ow sandbox` defaults."""
    return [
        Setting(
            key="sandbox.profile",
            type=str,
            default=None,
            help="Default harness profile for `ow sandbox` (auto, claude, opencode, codex, pi, omp, none)",
        ),
        Setting(
            key="sandbox.ro_paths",
            type=list,
            default=[],
            help="Extra host paths mounted read-only inside `ow sandbox`",
        ),
        Setting(
            key="sandbox.rw_paths",
            type=list,
            default=[],
            help="Extra host paths mounted read-write inside `ow sandbox`",
        ),
        Setting(
            key="sandbox.pass_env",
            type=list,
            default=[],
            help="Extra host environment variable names forwarded into `ow sandbox`",
        ),
    ]
