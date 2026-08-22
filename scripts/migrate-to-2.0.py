#!/usr/bin/env python3
"""
Migration script for ow 2.0.

Migrates from the 1.x project-scoped layout (ow.toml at a project root,
.bare-git-repos/ and workspaces/ beside it) to the 2.0 user-level layout
(XDG-based global config, shared bare repos, workspaces anywhere).

Automated:
  1. Copy ow.toml → ~/.config/ow/config.toml
  2. Move .bare-git-repos/ → ~/.local/share/ow/repos/
  3. Repair worktrees (git worktree repair per bare repo)
  4. Rename .ow/config → .ow/config.toml per workspace

Optional (--apply):
  5. Run ow apply per workspace (register in index + re-render templates)

Manual (guidance printed):
  - Templates: diff old customizations against packaged versions
  - See docs/migrating-to-2.0.md for full context

Usage:
    python scripts/migrate-to-2.0.py OLD              # dry-run: show plan
    python scripts/migrate-to-2.0.py OLD --yes        # execute
    python scripts/migrate-to-2.0.py OLD --yes --apply  # also run ow apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# --- XDG resolution (matches ow.utils.paths._base) ----------------------------

def _xdg(var: str, default: str) -> Path:
    """Resolve an XDG base directory, matching ow.utils.paths._base.

    A value that is not an absolute path is ignored, same as the spec.
    """
    value = os.environ.get(var)
    if not value or not Path(value).is_absolute():
        return Path.home() / default
    return Path(value)


def _abbrev(path: Path | str) -> str:
    """Abbreviate the home directory as ~ for display."""
    s = str(path)
    home = str(Path.home())
    if s.startswith(home):
        return "~" + s[len(home):]
    return s


# --- Steps --------------------------------------------------------------------

def step_config(toml_src: Path, target: Path, *, execute: bool) -> None:
    """Copy ow.toml → global config.toml."""
    print("1. Global config")
    if target.exists():
        print(f"   skip: {_abbrev(target)} already exists")
        return
    if execute:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(toml_src), str(target))
        print(f"   copied: {toml_src.name} → {_abbrev(target)}")
    else:
        print(f"   would copy: {toml_src.name} → {_abbrev(target)}")


def step_bare_repos(old: Path, repos_target: Path, *, execute: bool) -> None:
    """Move .bare-git-repos/*.git → repos dir, one repo at a time."""
    src = old / ".bare-git-repos"
    print("2. Bare repos")
    if not src.is_dir():
        print("   skip: no .bare-git-repos/ in $OLD")
        return
    repos = sorted(src.glob("*.git"))
    if not repos:
        print("   skip: no *.git directories found")
        return
    print(f"   {len(repos)} repo(s): {', '.join(r.name for r in repos)}")
    for repo in repos:
        dest = repos_target / repo.name
        if dest.exists():
            print(f"   skip: {repo.name} (already at target)")
        elif execute:
            repos_target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(repo), str(dest))
            print(f"   moved: {repo.name}")
        else:
            print(f"   would move: {repo.name}")


def step_repair(repos_dir: Path, *, execute: bool) -> None:
    """Run git worktree repair on each bare repo."""
    print("3. Worktree repair")
    if not repos_dir.is_dir():
        print("   skip: repos dir not found")
        return
    repos = sorted(repos_dir.glob("*.git"))
    if not repos:
        print("   skip: no repos found")
        return
    for repo in repos:
        if execute:
            result = subprocess.run(
                ["git", "-C", str(repo), "worktree", "repair"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"   repaired: {repo.name}")
                for line in result.stdout.strip().splitlines():
                    if line:
                        print(f"     {line}")
            else:
                print(f"   failed: {repo.name}: {result.stderr.strip()}")
        else:
            print(f"   would repair: {repo.name}")


def step_workspaces(old: Path, *, execute: bool) -> list[Path]:
    """Rename .ow/config → .ow/config.toml per workspace.

    Returns the list of workspace directories that were renamed (or would be).
    """
    ws_dir = old / "workspaces"
    print("4. Workspace configs")
    if not ws_dir.is_dir():
        print("   skip: no workspaces/ in $OLD")
        return []
    to_rename: list[tuple[Path, Path, Path]] = []
    for ws in sorted(ws_dir.iterdir()):
        if not ws.is_dir():
            continue
        old_cfg = ws / ".ow" / "config"
        new_cfg = ws / ".ow" / "config.toml"
        if new_cfg.exists():
            continue
        if not old_cfg.exists():
            continue
        to_rename.append((ws, old_cfg, new_cfg))
    if not to_rename:
        print("   skip: none to rename")
        return []
    print(f"   {len(to_rename)} workspace(s) to rename")
    for ws, old_cfg, new_cfg in to_rename:
        if execute:
            old_cfg.rename(new_cfg)
            print(f"   renamed: {ws.name}/.ow/config → .ow/config.toml")
        else:
            print(f"   would rename: {ws.name}/.ow/config → .ow/config.toml")
    return [ws for ws, _, _ in to_rename]


def step_apply(workspaces: list[Path], *, execute: bool) -> None:
    """Run ow apply per workspace."""
    print("5. Apply")
    if not workspaces:
        print("   skip: no workspaces")
        return
    ow_bin = shutil.which("ow")
    if not ow_bin:
        print("   skip: ow not on PATH (run 'ow apply <workspace>' manually)")
        return
    for ws in workspaces:
        if execute:
            print(f"   applying: {ws.name}...")
            result = subprocess.run([ow_bin, "apply", str(ws)])
            if result.returncode == 0:
                print(f"   done: {ws.name}")
            else:
                print(f"   failed: {ws.name} (exit {result.returncode})")
        else:
            print(f"   would run: ow apply {ws}")


def step_templates(old: Path) -> None:
    """Print template guidance (always manual)."""
    tmpl_dir = old / "templates"
    if not tmpl_dir.is_dir():
        return
    files = sorted(f for f in tmpl_dir.rglob("*") if f.is_file())
    if not files:
        return
    print(f"\nTemplates (manual — {len(files)} file(s) in $OLD/templates/):")
    for f in files:
        rel = f.relative_to(tmpl_dir)
        print(f"  {rel}")
        print(f"    ow templates --take {rel}")
        print(f"    diff {f} ~/.config/ow/templates/{rel}")


# --- Main ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate an ow 1.x project to the 2.0 user-level layout.",
    )
    parser.add_argument("old", help="old project root (directory holding ow.toml)")
    parser.add_argument(
        "--yes", action="store_true", help="execute the migration (default: dry-run)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="also run ow apply per workspace"
    )
    args = parser.parse_args()

    old = Path(args.old).resolve()
    if not old.is_dir():
        print(f"Error: {old} is not a directory", file=sys.stderr)
        sys.exit(1)

    toml = old / "ow.toml"
    if not toml.exists():
        toml = old / "ow.toml.example"
    if not toml.exists():
        print(f"Error: no ow.toml or ow.toml.example found in {old}", file=sys.stderr)
        sys.exit(1)

    config_dir = _xdg("XDG_CONFIG_HOME", ".config") / "ow"
    data_dir = _xdg("XDG_DATA_HOME", ".local/share") / "ow"
    global_config = config_dir / "config.toml"
    repos_dir = data_dir / "repos"

    mode = "EXECUTE" if args.yes else "DRY RUN"
    print(f"ow 2.0 migration — {old} ({mode})\n")

    step_config(toml, global_config, execute=args.yes)
    print()
    step_bare_repos(old, repos_dir, execute=args.yes)
    print()
    step_repair(repos_dir, execute=args.yes)
    print()
    workspaces = step_workspaces(old, execute=args.yes)
    print()
    if args.apply:
        step_apply(workspaces, execute=args.yes)
    elif workspaces:
        print("5. Apply (skipped — pass --apply to run ow apply per workspace)")
    print()

    step_templates(old)

    if not args.yes:
        print("\nPass --yes to execute.")
    else:
        print("\nMigration complete. Next steps:")
        print("  - Reopen your shell (mise drops stale OW_WORKSPACE)")
        if not args.apply:
            print("  - Run 'ow apply <workspace>' per workspace")
        print("  - Review templates guidance above")
        print("  - See docs/migrating-to-2.0.md for context")


if __name__ == "__main__":
    main()
