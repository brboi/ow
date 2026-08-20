"""`ow templates` — see what ow ships, take a file, hear when ow's moves.

Taking a file writes it twice: the working copy the user is free to edit, and
a pristine baseline. The baseline is what makes "did ow change this since I
took it?" answerable at all — diffing the user's copy against the packaged
file only ever shows the edits they made on purpose, which is why they took
it in the first place.
"""

import difflib
import shutil
import sys

from ow.utils import paths
from ow.utils.templates import (
    available_templates,
    packaged_files,
    resolve_template_files,
)

PACKAGED = "packaged"
TAKEN = "taken"
OUTDATED = "taken, outdated"


def _states() -> list[tuple[str, str]]:
    """Every available template file as (bundle/relpath, state), sorted."""
    entries: list[tuple[str, str]] = []
    for bundle in available_templates():
        packaged = packaged_files(bundle)
        for rel in resolve_template_files(bundle):
            name = f"{bundle}/{rel.as_posix()}"
            if not (paths.templates_dir() / bundle / rel).is_file():
                entries.append((name, PACKAGED))
                continue
            base = paths.template_base_dir() / bundle / rel
            src = packaged.get(rel.as_posix())
            # Byte for byte, baseline against packaged — never against the
            # user's copy. And no baseline means nothing to compare: a file
            # taken by hand stays `taken`, it never goes stale.
            outdated = (
                base.is_file()
                and src is not None
                and base.read_bytes() != src.read_bytes()
            )
            entries.append((name, OUTDATED if outdated else TAKEN))
    return sorted(entries)


def outdated_templates() -> list[str]:
    """`bundle/relpath` of every taken file whose packaged version has moved."""
    return [name for name, state in _states() if state == OUTDATED]


def _takeable_bundles() -> list[str]:
    """Bundles with something to take — a local-only bundle has nothing."""
    return [bundle for bundle in available_templates() if packaged_files(bundle)]


def _list() -> None:
    entries = _states()
    if not entries:
        print("No templates available.")
        return
    width = max(len(name) for name, _ in entries)
    for name, state in entries:
        print(f"{name.ljust(width)}  {state}")


def _take(name: str) -> None:
    bundle, _, rel = name.partition("/")
    src = packaged_files(bundle).get(rel) if bundle and rel else None
    if src is None:
        bundles = ", ".join(_takeable_bundles()) or "(none)"
        print(f"Error: no packaged template file named '{name}'.", file=sys.stderr)
        print(f"       Available bundles: {bundles}", file=sys.stderr)
        print("       Run `ow templates` to list every file.", file=sys.stderr)
        sys.exit(1)

    copy = paths.templates_dir() / bundle / rel
    baseline = paths.template_base_dir() / bundle / rel
    if copy.exists():
        # Overwriting would destroy the user's edits and, worse, silently
        # reset the baseline so the next diff reports nothing.
        print(f"Error: '{name}' is already taken: {copy}", file=sys.stderr)
        print(
            f"       Delete that file to take it again, or place a baseline "
            f"yourself at {baseline}.",
            file=sys.stderr,
        )
        sys.exit(1)

    for target in (copy, baseline):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    print(f"Took {name}")
    print(f"  yours    {copy}")
    print(f"  baseline {baseline}")


def _diff() -> None:
    for name in outdated_templates():
        bundle, _, rel = name.partition("/")
        baseline = paths.template_base_dir() / bundle / rel
        packaged = packaged_files(bundle)[rel]
        lines = difflib.unified_diff(
            baseline.read_text().splitlines(keepends=True),
            packaged.read_text().splitlines(keepends=True),
            fromfile=f"{name} (baseline)",
            tofile=f"{name} (packaged)",
        )
        for line in lines:
            print(line, end="" if line.endswith("\n") else "\n")


def cmd_templates(take: str | None = None, show_diff: bool = False) -> None:
    """List template files with their state, take one, or diff the stale ones."""
    if take is not None:
        # Taking writes a baseline identical to the packaged file, so there
        # would be nothing left for --diff to say about it.
        _take(take)
        return
    if show_diff:
        _diff()
        return
    _list()
