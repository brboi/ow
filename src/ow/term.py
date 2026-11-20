"""Terminal output utilities for ow.

In `--json` mode, human progress and plan lines written via `out()` are routed
to `stderr` so `stdout` carries exclusively the JSON document emitted by
`emit_json()`.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import sys
from collections.abc import Iterator
from typing import Any

_JSON_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar("_JSON_MODE", default=False)


@contextlib.contextmanager
def json_mode(enabled: bool) -> Iterator[None]:
    """Route `out()` to stderr and disable interactive prompts while `enabled` is True."""
    token = _JSON_MODE.set(enabled)
    try:
        yield
    finally:
        _JSON_MODE.reset(token)


def is_json_mode() -> bool:
    """Whether `--json` output mode is active."""
    return _JSON_MODE.get()


def emit_json(data: Any) -> None:
    """Write canonical UTF-8 JSON to stdout."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    sys.stdout.flush()


def out(text: str = "") -> None:
    """Write one plain line to stdout (or stderr while in `--json` mode)."""
    stream = sys.stderr if _JSON_MODE.get() else sys.stdout
    print(text, file=stream)


def warn(text: str) -> None:
    """Write a warning message to stderr."""
    print(f"warning: {text}", file=sys.stderr)


def error(text: str) -> None:
    """Write an error message to stderr."""
    print(f"error: {text}", file=sys.stderr)


def interactive() -> bool:
    """Check if stdin and stdout are both TTYs (always False in `--json` mode)."""
    if _JSON_MODE.get():
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str) -> bool:
    """Prompt for confirmation, return True if user accepts (y/yes), False on EOF."""
    stream = sys.stderr if _JSON_MODE.get() else sys.stdout
    stream.write(f"{question} [y/N] ")
    stream.flush()

    try:
        raw = sys.stdin.readline()
    except EOFError:
        stream.write("\n")
        return False
    except KeyboardInterrupt:
        stream.write("\n")
        raise

    if raw == "":  # EOF
        stream.write("\n")
        return False
    return raw.strip().lower() in ("y", "yes")
