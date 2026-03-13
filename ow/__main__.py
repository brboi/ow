#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import argcomplete

from ow.config import load_config
from ow.workspace import cmd_apply, cmd_create, cmd_rebase, cmd_remove, cmd_status


def find_root() -> Path:
    current = Path.cwd()
    while True:
        if (current / "ow.toml").exists() or (current / "ow.toml.example").exists():
            return current
        if current.parent == current:
            raise FileNotFoundError("ow.toml not found in current directory or any parent")
        current = current.parent


def main() -> None:
    parser = argparse.ArgumentParser(prog="ow", description="Odoo workspace manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_apply = subparsers.add_parser("apply", help="Apply configuration and create workspaces")
    p_apply.add_argument("name", nargs="?", help="Workspace name (applies all if omitted)")

    p_remove = subparsers.add_parser("remove", help="Remove a workspace")
    p_remove.add_argument("name")

    p_status = subparsers.add_parser("status", help="Show workspace status")
    p_status.add_argument("name", nargs="?", help="Workspace name (shows all if omitted)")

    p_create = subparsers.add_parser("create", help="Create a new workspace")
    p_create.add_argument("name")
    p_create.add_argument("repo_specs", nargs="+", metavar="alias:spec",
                          help="e.g. community:master enterprise:master..master-feature")

    p_rebase = subparsers.add_parser("rebase", help="Fetch and rebase workspace branches")
    p_rebase.add_argument("name")

    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    try:
        root = find_root()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    toml_path = root / "ow.toml"
    example_path = root / "ow.toml.example"
    if not toml_path.exists() and example_path.exists():
        shutil.copy(example_path, toml_path)
        print("Copied ow.toml.example → ow.toml. Edit it to configure your workspaces.")

    config = load_config(root / "ow.toml")

    if args.command == "apply":
        cmd_apply(config, args.name)
    elif args.command == "remove":
        cmd_remove(config, args.name)
    elif args.command == "status":
        cmd_status(config, args.name)
    elif args.command == "create":
        cmd_create(config, args.name, args.repo_specs)
    elif args.command == "rebase":
        cmd_rebase(config, args.name)


if __name__ == "__main__":
    main()
