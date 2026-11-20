"""`ow run` — execute a workspace (or resolve/create one on the fly and run it)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands import add as add_cmd
from ow.commands._shared import known_repo_catalog
from ow.core import git, index
from ow.core.plugins import RunCommand
from ow.core.session import open_session
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace


def _find_existing_workspace(name: str, *, base_cwd: Path) -> Path | None:
    """Return an existing workspace matching `name` (CWD child first, then index), or None."""
    local = base_cwd / name
    if (local / ".ow" / "config.toml").exists():
        return local.resolve()
    matches = [
        entry.resolve()
        for entry in index.read()
        if entry.name == name and (entry / ".ow" / "config.toml").exists()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        paths = ", ".join(str(p) for p in matches)
        raise UsageError(f"workspace {name!r} is ambiguous; matches: {paths}")
    return None


def _cleanup_ephemeral(root: Path, plugins: Plugins) -> None:
    """Remove worktrees registered in bare repos and delete the temporary workspace directory."""
    try:
        session = open_session(str(root), need_workspace=False, plugins=plugins)
        for loc in session.locations:
            git.run(["git", "-C", str(loc.bare), "worktree", "remove", "--force", str(loc.path)])
            git.run(["git", "-C", str(loc.bare), "worktree", "prune"])
    except Exception:
        pass
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _select_runner(ws: Workspace, plugins: Plugins, args: tuple[str, ...]) -> RunCommand:
    """Return the first RunCommand contributed by an enabled plugin for `ws`."""
    for _plugin, answer in plugins.call("ow_runner", ws=ws, args=args):
        if isinstance(answer, RunCommand):
            return answer
    raise UsageError(
        f"no runner available for workspace {ws.name!r}; ensure a runnable plugin (e.g. odoo) is enabled"
    )


def run(
    plugins: Plugins,
    *,
    target: str | None = None,
    workspace: str | None = None,
    tmp: bool = False,
    extra_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Run the workspace via its plugin runner; creates/reuses workspace when `target` is given."""
    if target is not None and workspace is not None:
        raise UsageError("pass either TARGET or -w/--workspace, not both")

    base_cwd = (cwd or Path.cwd()).resolve()
    ephemeral_parent: Path | None = None
    ephemeral_root: Path | None = None

    try:
        if target is None:
            if tmp:
                raise UsageError("--tmp requires a TARGET (e.g. `ow run enterprise#12345 --tmp`)")
            session = open_session(workspace, need_workspace=True, plugins=plugins, cwd=base_cwd)
            for warning in session.warnings:
                term.warn(warning)
            assert session.workspace is not None
            ws = session.workspace
        else:
            catalog = known_repo_catalog(plugins)
            settings = add_cmd._global_settings(plugins)
            resolved = add_cmd._resolve_composite_target(target, plugins, catalog, settings)
            canonical_name = resolved.name if resolved is not None else target

            if tmp:
                ephemeral_parent = Path(tempfile.mkdtemp(prefix="ow-run-"))
                created, code = add_cmd.create_workspace(
                    plugins,
                    parent_dir=ephemeral_parent,
                    target=target,
                    register_index=False,
                    resolved=resolved,
                    detach=True,
                )
                ephemeral_root = created.root
                if code != 0:
                    return code
                session = open_session(str(created.root), need_workspace=True, plugins=plugins)
                for warning in session.warnings:
                    term.warn(warning)
                assert session.workspace is not None
                ws = session.workspace
            else:
                existing = _find_existing_workspace(canonical_name, base_cwd=base_cwd)

                if existing is not None:
                    session = open_session(str(existing), need_workspace=True, plugins=plugins)
                    for warning in session.warnings:
                        term.warn(warning)
                    assert session.workspace is not None
                    ws = session.workspace
                else:
                    created, code = add_cmd.create_workspace(
                        plugins,
                        parent_dir=base_cwd,
                        target=target,
                        register_index=True,
                        resolved=resolved,
                    )
                    if code != 0:
                        return code
                    session = open_session(str(created.root), need_workspace=True, plugins=plugins)
                    for warning in session.warnings:
                        term.warn(warning)
                    assert session.workspace is not None
                    ws = session.workspace

        cmd = _select_runner(ws, plugins, tuple(extra_args))
        env = dict(os.environ)
        env.update(cmd.env)
        try:
            proc = subprocess.run(cmd.argv, cwd=cmd.cwd, env=env)
        except KeyboardInterrupt:
            return 130
        except OSError as exc:
            raise UsageError(f"failed to execute {cmd.argv[0]!r}: {exc}") from exc
        if proc.returncode < 0:
            return 128 + abs(proc.returncode)
        return proc.returncode
    finally:
        try:
            if ephemeral_root is not None:
                _cleanup_ephemeral(ephemeral_root, plugins)
        finally:
            if ephemeral_parent is not None and ephemeral_parent.exists():
                shutil.rmtree(ephemeral_parent, ignore_errors=True)
