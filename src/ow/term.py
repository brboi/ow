"""Terminal output utilities for ow."""
from __future__ import annotations

import sys

from rich.console import Console

# Global console instances
console = Console(
    soft_wrap=True,
    highlight=False,
    markup=False,
    force_terminal=None,  # Auto-detect; stripped when piped
)

err_console = Console(
    stderr=True,
    soft_wrap=True,
    highlight=False,
    markup=False,
    force_terminal=None,  # Auto-detect; stripped when piped
)


def out(text: str = "") -> None:
    """Write one plain line to stdout."""
    console.print(text)


def warn(text: str) -> None:
    """Write a warning message to stderr."""
    err_console.print(f"warning: {text}")


def error(text: str) -> None:
    """Write an error message to stderr."""
    err_console.print(f"error: {text}")


def interactive() -> bool:
    """Check if stdin and stdout are both TTYs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str) -> bool:
    """Prompt for confirmation, return True if user accepts (y/yes), False on EOF."""
    sys.stdout.write(f"{question} [y/N] ")
    sys.stdout.flush()
    
    try:
        response = sys.stdin.readline().strip().lower()
        if not response:  # EOF returns empty string
            return False
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
