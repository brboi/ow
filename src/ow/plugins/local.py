"""Local plugin: copies seed files into .local/ on workspace creation."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core.paths import local_seed_dir
from ow.core.plugins import hookimpl

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


def _is_symlinked(path: Path, stop: Path) -> bool:
    """Whether `path` or a component between it and `stop` (exclusive) is a symlink."""
    current = path
    while current != stop and current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _copy_once(src: Path, dst: Path) -> None:
    """Copy `src` to an absent `dst` through a same-dir temp and `os.replace`."""
    with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dst)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@hookimpl
def ow_workspace_created(ws: Workspace) -> None:
    """Copy missing regular files from seed dir into .local/."""
    seed = local_seed_dir()
    if not seed.is_dir():
        return

    target = ws.root / ".local"

    if _is_symlinked(target, ws.root):
        term.warn(f"skipping seed copy: {target} is a symlink; make it a directory before `ow init`")
        return

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        term.warn(f"cannot create {target}: {e}")
        return

    for dirpath, dirnames, filenames in os.walk(seed, followlinks=False):
        for name in list(dirnames):
            src_dir = Path(dirpath) / name
            if src_dir.is_symlink():
                term.warn(f"skipping symlink seed source {src_dir}")
                dirnames.remove(name)

        target_dir = target / Path(dirpath).relative_to(seed)

        if _is_symlinked(target_dir, ws.root):
            term.warn(f"skipping seed directory {target_dir}: a .local ancestor is a symlink")
            continue

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            term.warn(f"cannot create {target_dir}: {e}")
            continue

        for filename in filenames:
            src_file = Path(dirpath) / filename
            dst_file = target_dir / filename

            try:
                if src_file.is_symlink():
                    term.warn(f"skipping symlink seed source {src_file}")
                    continue
                if not stat.S_ISREG(src_file.lstat().st_mode):
                    continue
                if dst_file.is_symlink():
                    term.warn(f"skipping {dst_file}: destination is a symlink")
                    continue
                if dst_file.exists():
                    continue
                _copy_once(src_file, dst_file)
            except OSError as e:
                term.warn(f"cannot copy seed file {src_file}: {e}")
