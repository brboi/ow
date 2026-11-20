"""Promote a mono-stack workspace into a multi-stack workspace."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from ow.commands._shared import print_dropped, refresh_workspace, remotes_of
from ow.commands._workspace import pin_stack_values
from ow.core import bases, config, index, stacks
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, Step, run_batch
from ow.core.session import Session, global_settings, open_session
from ow.errors import UsageError


def infer_existing_stack_name(
    session: Session,
    taken: set[str],
    explicit: str | None = None,
) -> str:
    """Choose the stack directory name for an existing mono-stack workspace being promoted."""
    if explicit is not None:
        cleaned = stacks.validate_name(explicit, kind="stack")
        if cleaned in taken:
            raise UsageError(
                f"--as {cleaned!r} collides with one of the new stack names; choose a different name"
            )
        return cleaned

    ws = session.workspace
    assert ws is not None

    feature_branches: list[str] = []
    for loc in session.locations:
        if not loc.branch:
            continue
        base = loc.explicit_base or loc.default_base
        is_base = (
            bases.is_base_branch_name(loc.branch, base)
            or session.plugins.first("ow_is_base_branch", ws=ws, repo=loc.name, branch=loc.branch)
            is True
        )
        if not is_base and loc.branch not in feature_branches:
            feature_branches.append(loc.branch)

    candidate: str | None = None
    if len(feature_branches) == 1:
        candidate = stacks.sanitize_slug(feature_branches[0], "main")
    else:
        suggested = session.plugins.first("ow_stack_name", ws=ws)
        if isinstance(suggested, str) and suggested.strip():
            candidate = stacks.sanitize_slug(suggested.strip(), "main")
    if candidate is None:
        if len(session.locations) == 1:
            candidate = stacks.sanitize_slug(session.locations[0].name, "main")
        else:
            candidate = "main"

    if candidate not in taken:
        return candidate
    idx = 1
    while f"{candidate}-{idx}" in taken:
        idx += 1
    return f"{candidate}-{idx}"


def promote_to_multi_workspace(
    session: Session,
    *,
    existing_name: str,
    new_names: Sequence[str],
    dry_run: bool,
    yes: bool,
    pending: list[dict[str, object]],
) -> tuple[Path | None, int]:
    """Promote a mono-stack workspace `session` into a multi-stack workspace.

    Displays the exact plan and confirms with `[y/N]` before moving files.
    Pins plugin-provided stack values (such as database name and ports) and appends the promoted
    stack's settings to `pending`, so stacks added afterwards (even in a dry run) avoid its ports.
    Repairs git worktree links, marks `[workspace].stack = true`, and refreshes.
    Returns `(moved_stack_root, exit_code)` (`moved_stack_root` is None on dry-run/failure).
    """
    assert session.workspace is not None
    assert session.config_path is not None
    ws = session.workspace
    root = ws.root.resolve()

    for loc in session.locations:
        if loc.problem == "forgotten":
            raise UsageError(
                f"cannot promote {ws.name!r}: {loc.name} has a forgotten worktree; "
                "remove or recreate it first"
            )

    dest = root / existing_name
    staging = root / ".ow-promote-tmp"
    if staging.exists():
        raise UsageError(f"temporary staging directory already exists: {staging}")

    # Capture plugin-pinned settings before moving so the promoted stack keeps
    # its exact local database/ports and avoids collisions with newly added stacks.
    preserved_values = dict(session.workspace_layer.values)
    newly_pinned = pin_stack_values(
        session.plugins,
        preserved_values,
        ws=ws,
        siblings=(),
        pending=pending,
        global_settings=global_settings(session.plugins),
    )

    entries_to_move = sorted(
        (p for p in root.iterdir() if p.name != staging.name),
        key=lambda p: p.name,
    )

    def _move_existing() -> None:
        staging.mkdir(parents=False, exist_ok=False)
        for entry in entries_to_move:
            shutil.move(str(entry), str(staging / entry.name))
        staging.rename(dest)

    def _update_config_and_refresh() -> None:
        dest_cfg = dest / ".ow" / "config.toml"
        dropped = config.write_workspace(
            dest_cfg,
            dict(session.accepted_bases),
            preserved_values,
            declared=session.declared,
            remotes_of=remotes_of,
            stack=True,
        )
        print_dropped(dropped)
        index.add(root)
        reloaded = open_session(str(dest), need_workspace=True, plugins=session.plugins, cwd=dest)
        refresh_workspace(reloaded)

    steps: list[Step] = [
        OwStep(
            shown=f"# move existing workspace contents into {existing_name}/",
            run=_move_existing,
        ),
    ]
    for loc in session.locations:
        new_wt = dest / loc.path.name
        steps.append(
            GitStep(("git", "-C", str(loc.bare), "worktree", "repair", str(new_wt)))
        )

    pinned_notes = [f"{k} = {v!r}" for k, v in newly_pinned.items() if isinstance(v, str)]
    pin_note = f" (preserving {', '.join(pinned_notes)})" if pinned_notes else ""
    steps.append(
        OwStep(
            shown=f"# mark {existing_name}/ as a stack{pin_note} and refresh generated files",
            run=_update_config_and_refresh,
        )
    )

    notes = (
        f"promoting mono-stack workspace {root.name!r} to multi-stack ({existing_name}/ + {', '.join(f'{n}/' for n in new_names)})",
    )
    outcome = run_batch(
        Batch(
            root=root,
            repos=(RepoPlan(repo=root.name, steps=tuple(steps), notes=notes),),
        ),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    if dry_run or not outcome.mutated or outcome.exit_code != 0:
        return None, outcome.exit_code
    return dest, 0
