"""Local plugin: copies seed files into .local/ on workspace creation."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core.paths import local_seed_dir
from ow.core.plugins import hookimpl

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@hookimpl
def ow_workspace_created(ws: Workspace) -> None:
    """Copy missing regular files from seed dir into .local/."""
    seed = local_seed_dir()
    if not seed.is_dir():
        return

    target = ws.root / ".local"
    target.mkdir(parents=True, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(seed, followlinks=False):
        rel_dir = Path(dirpath).relative_to(seed)
        target_dir = target / rel_dir

        # Create directories as needed
        target_dir.mkdir(parents=True, exist_ok=True)

        # Copy regular files only, never overwriting
        for filename in filenames:
            src_file = Path(dirpath) / filename
            dst_file = target_dir / filename

            # Skip if destination exists
            if dst_file.exists():
                continue

            # Copy only regular files (via lstat, never following symlinks)
            try:
                st = src_file.lstat()
                if stat.S_ISREG(st.st_mode):
                    shutil.copy2(src_file, dst_file)
            except OSError:
                # Silently skip files we can't read
                pass
