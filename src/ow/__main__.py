"""Main entry point for ow CLI."""
from __future__ import annotations

import sys

from ow.cli import build_app
from ow.core.plugins import load_plugins
from ow.errors import OwError
from ow.term import error


def main() -> None:
    """Main entry point for the ow CLI."""
    try:
        plugins = load_plugins()
        app = build_app(plugins)
        app(prog_name="ow")
    except OwError as e:
        error(str(e))
        sys.exit(e.exit_code)


if __name__ == "__main__":
    main()
