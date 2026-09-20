"""Runtime prerequisites for a workspace's mise files.

Two things stand between ow and a workspace mise can use: a mise recent
enough to read `mise/conf.d/*.toml` (2026.8.13 is the floor — the release
that introduced visible fragments), and mise's explicit trust of the
fragment ow wrote. Both are checked here and nowhere else. Inspection never
runs mise at all, and the single call that changes state is `trust`, on one
file a render vouched for.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ow.utils.git import run_cmd

MISE_FLOOR = (2026, 8, 13)

_VERSION_TOKEN = re.compile(r"\d{4}\.\d+\.\d+")


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _requirement() -> str:
    return f"mise {_version_text(MISE_FLOOR)} or newer is required"


def require_mise() -> tuple[int, int, int]:
    """The running mise's version, or a `ValueError` saying why it is unusable.

    The first `YYYY.M.PATCH` token in `mise --version` is the whole parse:
    that is the shape mise has published since its date-versioned releases,
    and anything else cannot be compared against the floor.
    """
    try:
        result = run_cmd(
            ["mise", "--version"], quiet=True, capture_output=True, text=True, check=True
        )
    except OSError as exc:
        raise ValueError(f"{_requirement()}; could not run it: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{_requirement()}; 'mise --version' failed: {exc}") from exc

    match = _VERSION_TOKEN.search(result.stdout or "")
    if match is None:
        raise ValueError(
            f"{_requirement()}; no version in 'mise --version' output: {result.stdout.strip()!r}"
        )
    major, minor, patch = (int(part) for part in match.group().split("."))
    version = (major, minor, patch)
    if version < MISE_FLOOR:
        raise ValueError(f"{_requirement()}; found {_version_text(version)}")
    return version


def trust_fragment(fragment: Path) -> None:
    """Ask mise to trust one fragment, as one argument.

    The caller owns the policy — ow trusts only the fragment it wrote,
    updated, adopted, or found already equal to its proposal, and only
    after the surrounding render succeeded. The path is an argv element and
    never shell text, so a workspace path with spaces or quotes reaches
    mise whole.
    """
    run_cmd(["mise", "trust", str(fragment)], check=True)