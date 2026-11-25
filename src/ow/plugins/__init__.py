"""Built-in plugins for ow."""

from __future__ import annotations

import types


def builtin_plugins() -> tuple[tuple[str, types.ModuleType], ...]:
    """The built-in plugin modules, in the order their hooks run."""
    from ow.plugins import (
        github,
        local,
        mailsink,
        mise,
        odoo,
        postgres,
        runbot,
        sandbox,
        vscode,
        zed,
    )

    # Order matters: `Plugins.pipe` threads values in this order (odoo expands companion
    # repos before runbot pins them).
    return (
        ("mise", mise),
        ("odoo", odoo),
        ("postgres", postgres),
        ("mailsink", mailsink),
        ("github", github),
        ("runbot", runbot),
        ("vscode", vscode),
        ("zed", zed),
        ("local", local),
        ("sandbox", sandbox),
    )
