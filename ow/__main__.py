#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import argcomplete

from ow.config import Config, load_config, parse_branch_spec
from ow.workspace import (
    _available_templates,
    cmd_create,
    cmd_prune,
    cmd_rebase,
    cmd_status,
    cmd_update,
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


def _available_repo_aliases():
    """Return repo aliases from ow.toml in declaration order."""
    try:
        root = find_root()
        toml_path = root / "ow.toml"
        if toml_path.exists():
            cfg = load_config(toml_path)
            return list(cfg.remotes.keys())
    except FileNotFoundError:
        pass
    return []


def _complete_gen_templates(prefix, parsed_args, **kwargs):
    """Tab completion for -t/--template."""
    try:
        root = find_root()
        config = Config(vars={}, remotes={}, root_dir=root)
        templates = _available_templates(config)
    except (FileNotFoundError, Exception):
        templates = []
    return [t for t in templates if t.startswith(prefix)]


def _complete_gen_repos(prefix, parsed_args, **kwargs):
    """Tab completion for -r/--repo — offers unused aliases."""
    provided_aliases = set()
    if parsed_args.repo:
        for pair in parsed_args.repo:
            provided_aliases.add(pair[0])
    available = [a for a in _available_repo_aliases() if a not in provided_aliases]
    if available:
        return [a for a in available if a.startswith(prefix)]
    return []


def _complete_workspace_name(prefix, parsed_args, **kwargs):
    """Tab completion for workspace name."""
    try:
        root = find_root()
        workspaces_dir = root / "workspaces"
        if workspaces_dir.exists():
            names = [d.name for d in workspaces_dir.iterdir() if d.is_dir() and (d / ".ow" / "config").exists()]
            return [n for n in names if n.startswith(prefix)]
    except FileNotFoundError:
        pass
    return []


def main() -> None:
    parser = argparse.ArgumentParser(prog="ow", description="Odoo workspace manager")
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_create = subparsers.add_parser(
        "create", help="Create a new workspace"
    )
    p_create.add_argument(
        "-n", "--name", help="Workspace name"
    )
    p_create.add_argument(
        "-c", "--configuration", help="Path to existing workspace config to duplicate"
    )
    p_create.add_argument(
        "-t", "--template", nargs="*", metavar="TEMPLATE",
        help="Templates to apply (space-separated)",
    ).completer = _complete_gen_templates  # type: ignore[attr-defined]
    p_create.add_argument(
        "-r", "--repo", nargs=2, action="append", metavar=("ALIAS", "SPEC"),
        help="Repo alias and branch spec (repeatable, e.g. -r community master..x)",
    ).completer = _complete_gen_repos  # type: ignore[attr-defined]

    p_update = subparsers.add_parser(
        "update", help="Re-render templates and materialize worktrees"
    )
    p_update.add_argument(
        "workspace", nargs="?", help="Workspace name (default: resolve from cwd)"
    ).completer = _complete_workspace_name  # type: ignore[attr-defined]

    p_status = subparsers.add_parser("status", help="Show workspace status")
    p_status.add_argument(
        "workspace", nargs="?", help="Workspace name (default: resolve from cwd)"
    ).completer = _complete_workspace_name  # type: ignore[attr-defined]

    p_rebase = subparsers.add_parser(
        "rebase", help="Fetch and rebase workspace branches"
    )
    p_rebase.add_argument(
        "workspace", nargs="?", help="Workspace name (default: resolve from cwd)"
    ).completer = _complete_workspace_name  # type: ignore[attr-defined]

    p_prune = subparsers.add_parser(
        "prune", help="Clean up stale worktree references and orphaned branches"
    )

    # Tab completion for create subcommand
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
"""
        toml_path.write_text(minimal_config)
        print("Created ow.toml with default configuration. Edit it to suit your needs.")

    config = load_config(toml_path)

    if args.command == "create":
        # Parse repo pairs from --repo args
        repo_pairs = {}
        if args.repo:
            for alias, spec in args.repo:
                repo_pairs[alias] = parse_branch_spec(spec)

        cmd_create(config, name=args.name, templates=args.template, repos=repo_pairs, configuration=args.configuration)
    elif args.command == "update":
        cmd_update(config, workspace=args.workspace)
    elif args.command == "status":
        cmd_status(config, workspace=args.workspace)
    elif args.command == "rebase":
        cmd_rebase(config, workspace=args.workspace)
    elif args.command == "prune":
        cmd_prune(config)


if __name__ == "__main__":
    main()
