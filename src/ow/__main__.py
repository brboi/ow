"""Main entry point for ow CLI."""
from __future__ import annotations

import sys

from ow.cli import build_app, fail
from ow.core.plugins import load_plugins
from ow.errors import OwError


def main() -> None:
    try:
        app = build_app(load_plugins())
        app(prog_name="ow")
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
    except OwError as exc:
        sys.exit(fail(exc))


if __name__ == "__main__":
    main()
