#!/usr/bin/env python3
"""
Migration script for ow v1.0.0.

Migrates from the central declarative model (workspaces in ow.toml)
to the per-workspace config model (.ow/config files).

This script:
1. Backs up ow.toml
2. Reads [repo] as global defaults, then [[workspace]] sections
3. Merges repos: global defaults + workspace-specific overrides
4. Creates workspaces/<name>/.ow/config for each workspace
5. Removes [[workspace]] sections from ow.toml

Run from the ow project root:
    python scripts/migrate-to-1.0.0.py
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import tomli_w


def merge_repos(global_repos: dict, ws_repos: dict) -> dict[str, str]:
    """Merge global repo defaults with workspace-specific overrides."""
    merged = dict(global_repos)
    merged.update(ws_repos)
    return merged


def backup_toml(toml_path: Path) -> Path:
    """Create a timestamped backup of ow.toml."""
    backup_path = toml_path.with_suffix(".toml.bak")
    if backup_path.exists():
        # Increment backup name if .bak already exists
        i = 1
        while (alt := toml_path.with_suffix(f".toml.bak.{i}")).exists():
            i += 1
        backup_path = alt
    shutil.copy2(toml_path, backup_path)
    print(f"Backup: {backup_path}")
    return backup_path


def write_workspace_config(ws_dir: Path, name: str, templates: list[str], repos: dict[str, str], vars: dict) -> None:
    """Write .ow/config file for a workspace."""
    config_dir = ws_dir / ".ow"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config"

    if config_path.exists():
        print(f"  Skipping {name}: .ow/config already exists")
        return

    data: dict = {"templates": templates, "repos": repos}
    if vars:
        data["vars"] = vars

    with open(config_path, "wb") as f:
        tomli_w.dump(data, f)

    print(f"  Created {config_path}")


def remove_workspace_sections(toml_path: Path) -> None:
    """Remove [[workspace]] sections from ow.toml, keeping other sections.

    Uses a regex that handles whitespace inside brackets (e.g. [[ workspace ]])
    and preserves all other content byte-for-byte including comments.
    """
    lines = toml_path.read_text().split("\n")
    new_lines: list[str] = []
    i = 0
    ws_header_re = re.compile(r"^\s*\[\[\s*workspace\s*\]\]\s*$")
    section_header_re = re.compile(r"^\s*\[")

    while i < len(lines):
        if ws_header_re.match(lines[i]):
            i += 1
            while i < len(lines):
                if section_header_re.match(lines[i]):
                    break
                i += 1
        else:
            new_lines.append(lines[i])
            i += 1

    # Clean up trailing empty lines
    while new_lines and not new_lines[-1].strip():
        new_lines.pop()

    toml_path.write_text("\n".join(new_lines) + "\n")


def main() -> None:
    root = Path.cwd()
    toml_path = root / "ow.toml"
    if not toml_path.exists():
        print("Error: ow.toml not found. Run from ow project root.", file=sys.stderr)
        sys.exit(1)

    workspaces_dir = root / "workspaces"
    if not workspaces_dir.exists():
        print("Error: workspaces/ directory not found.", file=sys.stderr)
        sys.exit(1)

    # Parse ow.toml
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    # Global repo defaults from [repo]
    global_repos = data.get("repo", {})

    # Workspace sections
    old_workspaces = data.get("workspace", [])
    if not old_workspaces:
        print("No [[workspace]] sections found in ow.toml. Nothing to migrate.")
        return

    print(f"Found {len(old_workspaces)} workspace(s) to migrate:\n")

    # Backup before any modifications
    backup_path = backup_toml(toml_path)

    # Create .ow/config for each workspace
    for ws_data in old_workspaces:
        name = ws_data.get("name")
        if not name:
            print("  Warning: workspace without name, skipping")
            continue

        ws_dir = workspaces_dir / name
        if not ws_dir.exists():
            print(f"  Warning: workspace directory '{name}' not found, skipping")
            continue

        # Merge global repos with workspace-specific overrides
        repos = merge_repos(global_repos, ws_data.get("repo", {}))

        # Templates (empty list if not specified — user can add later)
        templates = ws_data.get("templates", [])

        # Vars from vars.* dotted keys
        vars = ws_data.get("vars", {})

        write_workspace_config(ws_dir, name, templates, repos, vars)

    # Remove [[workspace]] sections from ow.toml
    print("\nRemoving [[workspace]] sections from ow.toml...")
    remove_workspace_sections(toml_path)
    print("Done.")

    print("\nMigration complete. You can now:")
    print("  - Run 'ow status' from any workspace directory")
    print("  - Delete this script: rm scripts/migrate-to-1.0.0.py")
    print(f"  - Restore backup if needed: cp {backup_path} {toml_path}")


if __name__ == "__main__":
    main()
