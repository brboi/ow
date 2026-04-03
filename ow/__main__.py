#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import argcomplete

from ow.config import load_config
from ow.workspace import (
    cmd_apply,
    cmd_create,
    cmd_rebase,
    cmd_remove,
    cmd_status,
)

try:
    from ow._version import version as __version__
except ImportError:
    __version__ = "dev"


def find_root() -> Path:
    current = Path.cwd()
    while True:
        if (current / "ow.toml").exists() or (current / "ow.toml.example").exists():
            return current
        if current.parent == current:
            raise FileNotFoundError(
                "ow.toml not found in current directory or any parent"
            )
        current = current.parent


def workspace_completer(prefix, parsed_args, **kwargs):
    try:
        root = find_root()
        cfg = load_config(root / "ow.toml")
        return [ws.name for ws in cfg.workspaces if ws.name.startswith(prefix)]
    except Exception:
        return []


def resolve_workspace_name(name: str | None, allow_all: bool = False) -> str | None:
    """Return workspace name, or None meaning 'all'.
    Raises SystemExit if name is required but missing."""
    if name:
        return name
    env_name = os.environ.get("OW_WORKSPACE")
    if env_name:
        return env_name
    if allow_all:
        return None
    print(
        "Error: workspace name required (or run from inside a workspace with mise activated)",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ow", description="Odoo workspace manager")
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_apply = subparsers.add_parser(
        "apply", help="Apply configuration and create workspaces"
    )
    p_apply.add_argument(
        "name", nargs="?", help="Workspace name (applies all if omitted)"
    ).completer = workspace_completer

    p_remove = subparsers.add_parser("remove", help="Remove a workspace")
    p_remove.add_argument("name").completer = workspace_completer

    p_status = subparsers.add_parser("status", help="Show workspace status")
    p_status.add_argument(
        "name", nargs="?", help="Workspace name (shows all if omitted)"
    ).completer = workspace_completer
    p_status.add_argument(
        "--all",
        action="store_true",
        dest="all_workspaces",
        help="Show all workspaces (overrides OW_WORKSPACE)",
    )

    p_create = subparsers.add_parser("create", help="Create a new workspace")
    p_create.add_argument("name")
    p_create.add_argument(
        "specs",
        nargs="+",
        metavar="alias:spec|vars.key=value",
        help="e.g. community:master vars.http_port=8080",
    )

    p_rebase = subparsers.add_parser(
        "rebase", help="Fetch and rebase workspace branches"
    )
    p_rebase.add_argument("name", nargs="?").completer = workspace_completer

    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    try:
        root = find_root()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    toml_path = root / "ow.toml"
    if not toml_path.exists():
        minimal_config = """\
[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"

[[workspace]]
name = "master"
repo.community = "master"
"""
        toml_path.write_text(minimal_config)
        print("Created ow.toml with default configuration. Edit it to suit your needs.")

    config = load_config(toml_path)

    if args.command == "apply":
        cmd_apply(config, args.name)
    elif args.command == "remove":
        cmd_remove(config, args.name)
    elif args.command == "status":
        name = (
            None
            if getattr(args, "all_workspaces", False)
            else resolve_workspace_name(args.name, allow_all=True)
        )
        cmd_status(config, name)
    elif args.command == "create":
        cmd_create(config, args.name, args.specs)
    elif args.command == "rebase":
        name = resolve_workspace_name(args.name)
        cmd_rebase(config, name)


if __name__ == "__main__":
    main()
