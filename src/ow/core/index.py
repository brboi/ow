"""Workspace index file operations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ow.core.paths import index_file


def read() -> tuple[Path, ...]:
    """Read the workspace index file.
    
    Returns a tuple of absolute paths, in order, with duplicates and blank lines ignored.
    Returns an empty tuple if the file doesn't exist.
    """
    index_path = index_file()
    
    if not index_path.exists():
        return ()
    
    seen = set()
    result = []
    
    with open(index_path, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            # Skip blank lines
            if not line or not line.strip():
                continue
            
            path = Path(line)
            abs_path = Path(os.path.abspath(path))
            
            # Skip duplicates, keeping first occurrence
            abs_str = str(abs_path)
            if abs_str not in seen:
                seen.add(abs_str)
                result.append(abs_path)
    
    return tuple(result)


def add(path: Path) -> None:
    """Add a workspace path to the index.
    
    Idempotent: does nothing if the path is already in the index.
    Atomic: uses a temp file + os.replace in the same directory.
    Creates parent directories as needed.
    """
    abs_path = Path(os.path.abspath(path))
    index_path = index_file()
    
    # Create parent directories
    index_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Read current entries
    current = read()
    
    # Check if already present
    if abs_path in current:
        return
    
    # Add to list
    new_entries = list(current) + [abs_path]
    
    # Write atomically using a temp file
    temp_fd, temp_path = tempfile.mkstemp(
        dir=index_path.parent,
        prefix=".tmp-",
        suffix=".workspaces",
    )
    try:
        with os.fdopen(temp_fd, "w") as f:
            for entry in new_entries:
                f.write(str(entry) + "\n")
        os.replace(temp_path, index_path)
    except Exception:
        os.unlink(temp_path)
        raise


def remove(path: Path) -> None:
    """Remove a workspace path from the index.
    
    No-op if the path is not in the index.
    """
    abs_path = Path(os.path.abspath(path))
    index_path = index_file()
    
    if not index_path.exists():
        return
    
    # Read current entries
    current = read()
    
    # Check if present
    if abs_path not in current:
        return
    
    # Remove from list
    new_entries = [p for p in current if p != abs_path]
    
    # Write atomically using a temp file
    temp_fd, temp_path = tempfile.mkstemp(
        dir=index_path.parent,
        prefix=".tmp-",
        suffix=".workspaces",
    )
    try:
        with os.fdopen(temp_fd, "w") as f:
            for entry in new_entries:
                f.write(str(entry) + "\n")
        os.replace(temp_path, index_path)
    except Exception:
        os.unlink(temp_path)
        raise
