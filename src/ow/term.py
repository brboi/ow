"""Terminal output utilities for ow."""
from __future__ import annotations

import sys


def out(text: str = "") -> None:
    """Write one plain line to stdout."""
    print(text)


def err(text: str) -> None:
    """Write one plain line to stderr."""
    print(text, file=sys.stderr)


def warn(text: str) -> None:
    """Write a warning message to stderr."""
    print(f"warning: {text}", file=sys.stderr)


def error(text: str) -> None:
    """Write an error message to stderr."""
    print(f"error: {text}", file=sys.stderr)


def interactive() -> bool:
    """Check if stdin and stdout are both TTYs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str) -> bool:
    """Prompt for confirmation, return True if user accepts (y/yes), False on EOF."""
    sys.stdout.write(f"{question} [y/N] ")
    sys.stdout.flush()

    try:
        raw = sys.stdin.readline()
    except EOFError:
        sys.stdout.write("\n")
        return False
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        raise

    if raw == "":  # EOF
        sys.stdout.write("\n")
        return False
    return raw.strip().lower() in ("y", "yes")
