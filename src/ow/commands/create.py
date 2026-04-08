import re
import sys
from pathlib import Path
from typing import Any

import questionary

from ow.utils.templates import (
    apply_templates,
    available_templates,
    ensure_workspace_materialized,
)
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    load_workspace_config,
    parse_branch_spec,
    write_workspace_config,
)
from ow.utils.git import run_cmd

# ---------------------------------------------------------------------------
# Internal helpers for cmd_create
# ---------------------------------------------------------------------------


def _cleanup_failed_workspace(ws_dir) -> None:
    """Remove workspace directory if it's empty or contains only .ow/."""
    import shutil
    if not ws_dir.exists():
        return
    contents = list(ws_dir.iterdir())
    if not contents or contents == [ws_dir / ".ow"]:
        shutil.rmtree(ws_dir)


def _validate_create_inputs(
    config: Config,
    name: str | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
    configuration: str | None,
) -> tuple[WorkspaceConfig | None, str, Any]:
    """Validate CLI inputs and resolve source workspace if duplicating.

    Returns (source_ws, resolved_name, ws_dir).
    Exits on validation errors.
    """
    templates_root = config.root_dir / "templates"
    if not templates_root.exists():
        print("Error: templates/ directory not found.", file=sys.stderr)
        sys.exit(1)
    available = sorted(
        d.name
        for d in templates_root.iterdir()
        if d.is_dir()
    )
    if not available:
        print("Error: no templates found in templates/", file=sys.stderr)
        sys.exit(1)

    if templates is not None:
        invalid = [t for t in templates if t not in available]
        if invalid:
            avail = ", ".join(available)
            print(f"Error: unknown template(s): {', '.join(invalid)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

    known_aliases = list(config.remotes.keys())
    if repos is not None:
        unknown = [a for a in repos if a not in known_aliases]
        if unknown:
            avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
            print(f"Error: unknown repo alias(es): {', '.join(unknown)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

    source_ws: WorkspaceConfig | None = None
    if configuration is not None:
        src_path = Path(configuration)
        if src_path.is_dir():
            src_config_file = src_path / ".ow" / "config"
        else:
            src_config_file = src_path
        if not src_config_file.exists():
            print(f"Error: configuration file not found: {src_config_file}", file=sys.stderr)
            sys.exit(1)
        source_ws = load_workspace_config(src_config_file)

        avail_templates = available_templates(config)
        invalid = [t for t in source_ws.templates if t not in avail_templates]
        if invalid:
            avail = ", ".join(avail_templates) if avail_templates else "(none found)"
            print(f"Error: configuration references unknown template(s): {', '.join(invalid)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

        for alias in source_ws.repos:
            if alias not in known_aliases:
                avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
                print(f"Error: configuration references repo '{alias}' but it's not defined in ow.toml [remotes]", file=sys.stderr)
                print(f"  Available remotes: {avail}", file=sys.stderr)
                sys.exit(1)

    if name is not None:
        name = name.strip()
        if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
            print("Error: name must be alphanumeric with hyphens and underscores only.", file=sys.stderr)
            sys.exit(1)
        ws_dir = config.root_dir / "workspaces" / name
        if ws_dir.exists():
            print(f"Error: workspace '{name}' already exists at {ws_dir}.", file=sys.stderr)
            sys.exit(1)
    else:
        while True:
            try:
                name = questionary.text("Workspace name").ask()
            except KeyboardInterrupt:
                print("\nAborted.", file=sys.stderr)
                sys.exit(1)
            if not name:
                print("Error: name is required.", file=sys.stderr)
                sys.exit(1)
            name = name.strip()
            if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
                print("Error: name must be alphanumeric with hyphens and underscores only.", file=sys.stderr)
                continue
            ws_dir = config.root_dir / "workspaces" / name
            if ws_dir.exists():
                print(f"Warning: workspace '{name}' already exists at {ws_dir}. Choose another name.")
                continue
            break

    return source_ws, name, ws_dir


def _gather_workspace_config_interactive(
    config: Config,
    source_ws: WorkspaceConfig | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
) -> WorkspaceConfig:
    """Run interactive questionnaire to build WorkspaceConfig.

    Pre-populates from source_ws or CLI args where available.
    """
    available_t = sorted(
        d.name
        for d in (config.root_dir / "templates").iterdir()
        if d.is_dir()
    )
    known_aliases = list(config.remotes.keys())

    if source_ws is not None:
        pre_selected = set(source_ws.templates)
        if templates is not None:
            pre_selected = set(templates)
        final_repos: dict[str, BranchSpec] = dict(source_ws.repos)
        if repos is not None:
            final_repos.update(repos)
    else:
        pre_selected = set(templates) if templates else set()
        final_repos = dict(repos) if repos else {}

    _check_duplicate_branches(final_repos, config)

    try:
        selected_templates = questionary.checkbox(
            "Templates (space to select, enter to confirm)",
            choices=[questionary.Choice(t, checked=(t in pre_selected)) for t in available_t],
        ).ask()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    if not selected_templates:
        selected_templates = []

    if known_aliases:
        pre_selected_aliases = set(final_repos.keys())
        try:
            selected_aliases = questionary.checkbox(
                "Repos to include (space to select, enter to confirm)",
                choices=[questionary.Choice(a, checked=(a in pre_selected_aliases)) for a in known_aliases],
            ).ask()
        except KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if not selected_aliases:
            selected_aliases = []

        for alias in selected_aliases:
            if alias not in final_repos:
                try:
                    spec_str = questionary.text(
                        f"{alias} branch spec (e.g. master, master..my-feature)",
                    ).ask()
                except KeyboardInterrupt:
                    print("\nAborted.", file=sys.stderr)
                    sys.exit(1)
                if not spec_str:
                    print("Aborted.", file=sys.stderr)
                    sys.exit(1)
                try:
                    final_repos[alias] = parse_branch_spec(spec_str.strip())
                except ValueError as e:
                    print(f"Error: invalid branch spec '{spec_str.strip()}': {e}", file=sys.stderr)
                    sys.exit(1)

    _check_duplicate_branches(final_repos, config)

    ws_vars: dict[str, Any] = dict(source_ws.vars) if source_ws is not None else dict(config.vars)

    return WorkspaceConfig(repos=final_repos, templates=selected_templates, vars=ws_vars)


def _check_duplicate_branches(new_repos: dict[str, BranchSpec], config: Config) -> None:
    """Abort if any repo alias shares the same local_branch as an existing workspace.

    Only local_branches (the part after `..`) are checked — source branches don't conflict
    since git only prevents two worktrees on the same local branch.
    """
    ws_root = config.root_dir / "workspaces"
    if not ws_root.exists():
        return
    for existing_ws_dir in sorted(ws_root.iterdir()):
        if not existing_ws_dir.is_dir():
            continue
        ow_config = existing_ws_dir / ".ow" / "config"
        if not ow_config.exists():
            continue
        existing = load_workspace_config(ow_config)
        for alias, new_spec in new_repos.items():
            if alias in existing.repos:
                existing_spec = existing.repos[alias]
                new_target = new_spec.local_branch
                existing_target = existing_spec.local_branch
                if new_target and existing_target and new_target == existing_target:
                    print(f"Error: workspace '{existing_ws_dir.name}' already uses {alias}:{existing_spec.to_spec_str()}", file=sys.stderr)
                    print(f"  Target branch '{new_target}' is already in use. Each target branch must be unique.", file=sys.stderr)
                    sys.exit(1)


# ---------------------------------------------------------------------------
# Command: create
# ---------------------------------------------------------------------------


def cmd_create(
    config: Config,
    name: str | None = None,
    templates: list[str] | None = None,
    repos: dict[str, BranchSpec] | None = None,
    configuration: str | None = None,
) -> None:
    """Interactive workspace creation with questionary.

    Optional pre-populated values from CLI args:
      name: workspace name
      templates: list of template names to pre-select
      repos: dict of alias -> BranchSpec to pre-select
      configuration: path to existing workspace config to duplicate
    """
    # Phase 1: Validate inputs and resolve name
    source_ws, resolved_name, ws_dir = _validate_create_inputs(
        config, name, templates, repos, configuration
    )

    # Phase 2: Interactive questionnaire
    ws = _gather_workspace_config_interactive(config, source_ws, templates, repos)

    # Phase 3: Confirm
    print(f"\nWorkspace '{resolved_name}' will be created with:")
    print(f"  Templates: {', '.join(ws.templates)}")
    for alias, spec in ws.repos.items():
        print(f"  {alias}: {spec.to_spec_str()}")
    if ws.vars:
        print(f"  Vars: {ws.vars}")

    try:
        confirm = questionary.confirm("Proceed?").ask()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    if not confirm:
        print("Aborted.")
        return

    ow_config_path = ws_dir / ".ow" / "config"
    if ow_config_path.exists():
        print(f"Error: workspace '{resolved_name}' already exists at workspaces/{resolved_name}", file=sys.stderr)
        sys.exit(1)

    # Phase 4: Materialize
    _, successful, errors = ensure_workspace_materialized(ws, config, ws_dir)

    if errors:
        if len(errors) == len(ws.repos):
            _cleanup_failed_workspace(ws_dir)
            print(f"\nError: all repos failed to set up:", file=sys.stderr)
            for alias, err in errors.items():
                print(f"  {alias}: {err}", file=sys.stderr)
            sys.exit(1)

        print(f"\nWarning: some repos failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    apply_templates(ws, config, ws_dir)

    write_workspace_config(ow_config_path, ws)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        run_cmd(["mise", "trust", str(mise_toml)], check=True)

    if errors:
        print(f"\nWorkspace '{resolved_name}' created with errors. Fix issues and run: ow update")
    else:
        print(f"\nWorkspace '{resolved_name}' created. To install dependencies:")
        print(f"    cd workspaces/{resolved_name} && mise install")
    print(f"\nWorkspace config: {ow_config_path}")
    print("Edit it to customize vars, then run: ow update")
