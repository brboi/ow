"""Creating a workspace, here or in ./NAME.

Mirrors `git init`: a workspace is a directory holding a .ow/config.toml, and
its name is that directory's name — which is already what
`build_template_context` reads. Nothing here knows about a project root,
because there is none.
"""

import re
import shutil
import sys
from pathlib import Path
from typing import Any

import questionary

from ow.utils import index
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

MARKER = Path(".ow") / "config.toml"

# ---------------------------------------------------------------------------
# Internal helpers for cmd_init
# ---------------------------------------------------------------------------


def _cleanup_failed_workspace(ws_dir: Path) -> None:
    """Remove workspace directory if it's empty or contains only .ow/.

    Only ever called on a directory ow created itself. `ow init` with no
    argument runs in a directory the user was already standing in, and
    deleting that because a fetch failed would be unforgivable.
    """
    if not ws_dir.exists():
        return
    contents = list(ws_dir.iterdir())
    if not contents or contents == [ws_dir / ".ow"]:
        shutil.rmtree(ws_dir)


def _validate_init_inputs(
    config: Config,
    name: str | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
    configuration: str | None,
) -> tuple[WorkspaceConfig | None, Path]:
    """Validate CLI inputs and resolve the target directory.

    Returns (source_ws, ws_dir). Exits on validation errors.
    """
    # Packaged templates are always available even when the user hasn't taken
    # (and thus doesn't have a local copy of) any — nothing is copied at
    # bootstrap, so listing paths.templates_dir() alone would wrongly treat a
    # fresh install as having no templates.
    available = available_templates(config)

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
        src_config_file = src_path / MARKER if src_path.is_dir() else src_path
        if not src_config_file.exists():
            print(f"Error: configuration file not found: {src_config_file}", file=sys.stderr)
            sys.exit(1)
        source_ws = load_workspace_config(src_config_file)

        invalid = [t for t in source_ws.templates if t not in available]
        if invalid:
            avail = ", ".join(available) if available else "(none found)"
            print(f"Error: configuration references unknown template(s): {', '.join(invalid)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

        for alias in source_ws.repos:
            if alias not in known_aliases:
                avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
                print(f"Error: configuration references repo '{alias}' but it's not defined in [remotes]", file=sys.stderr)
                print(f"  Available remotes: {avail}", file=sys.stderr)
                sys.exit(1)

    if name is None:
        # No argument means "here", like `git init`. The charset rule below
        # does not apply: ow is not naming this directory, it is standing in
        # one the user already named.
        ws_dir = Path.cwd()
    else:
        name = name.strip()
        if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
            print("Error: name must be alphanumeric with hyphens and underscores only.", file=sys.stderr)
            sys.exit(1)
        ws_dir = Path.cwd() / name

    # A plain directory, empty or not, is just a place to work. The one thing
    # ow refuses to walk over is an existing workspace definition.
    if (ws_dir / MARKER).exists():
        print(f"Error: {ws_dir} is already a workspace.", file=sys.stderr)
        print(f"       {ws_dir / MARKER} exists; edit it and run `ow update`.", file=sys.stderr)
        sys.exit(1)

    return source_ws, ws_dir


def _preselection(
    source_ws: WorkspaceConfig | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
) -> tuple[list[str], dict[str, BranchSpec]]:
    """Everything the caller already decided, before any question is asked."""
    if source_ws is not None:
        chosen_templates = list(templates) if templates else list(source_ws.templates)
        chosen_repos: dict[str, BranchSpec] = dict(source_ws.repos)
    else:
        chosen_templates = list(templates) if templates else []
        chosen_repos = {}
    if repos:
        chosen_repos.update(repos)
    return chosen_templates, chosen_repos


def _workspace_config_from_flags(
    config: Config,
    source_ws: WorkspaceConfig | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
) -> WorkspaceConfig:
    """Build the workspace config without asking anything.

    Used when stdin is not a terminal. The flags have to carry everything;
    what they don't carry is named, not prompted for.
    """
    chosen_templates, chosen_repos = _preselection(source_ws, templates, repos)

    missing: list[str] = []
    if not chosen_templates:
        avail = ", ".join(available_templates(config))
        missing.append(f"-t/--template NAME     available: {avail}")
    if not chosen_repos:
        avail = ", ".join(config.remotes) or "(none configured)"
        missing.append(f"-r/--repo ALIAS:SPEC   aliases: {avail}")
    if missing:
        print("Error: stdin is not a terminal, so ow init cannot ask. Missing:", file=sys.stderr)
        for line in missing:
            print(f"         {line}", file=sys.stderr)
        print("       or pass -c/--configuration to copy an existing workspace.", file=sys.stderr)
        sys.exit(1)

    ws_vars = dict(source_ws.vars) if source_ws is not None else dict(config.vars)
    return WorkspaceConfig(repos=chosen_repos, templates=chosen_templates, vars=ws_vars)


def _gather_workspace_config_interactive(
    config: Config,
    source_ws: WorkspaceConfig | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
) -> WorkspaceConfig:
    """Run interactive questionnaire to build WorkspaceConfig.

    Pre-populates from source_ws or CLI args where available.
    """
    available_t = available_templates(config)
    known_aliases = list(config.remotes.keys())
    pre_selected_templates, final_repos = _preselection(source_ws, templates, repos)
    pre_selected = set(pre_selected_templates)

    # Fail on what the flags already say before making anyone answer questions.
    _check_duplicate_branches(final_repos)

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

    ws_vars: dict[str, Any] = dict(source_ws.vars) if source_ws is not None else dict(config.vars)

    return WorkspaceConfig(repos=final_repos, templates=selected_templates, vars=ws_vars)


def _check_duplicate_branches(new_repos: dict[str, BranchSpec]) -> None:
    """Abort if a repo alias would reuse the local branch of a known workspace.

    Only local branches (the part after `..`) can collide — source branches
    are shared freely, git only objects to two worktrees on one local branch.

    Best-effort by design: it reads index.known_workspaces(), so a workspace
    ow has never resolved is invisible here and slips through. That is
    acceptable, because git itself still refuses the second worktree. This
    check exists only to say so earlier, and in a sentence naming the
    workspace that already holds the branch.
    """
    for existing_ws_dir in index.known_workspaces():
        existing = load_workspace_config(existing_ws_dir / MARKER)
        for alias, new_spec in new_repos.items():
            if alias not in existing.repos:
                continue
            existing_spec = existing.repos[alias]
            new_target = new_spec.local_branch
            existing_target = existing_spec.local_branch
            if new_target and existing_target and new_target == existing_target:
                print(f"Error: workspace '{existing_ws_dir.name}' already uses {alias}:{existing_spec.to_spec_str()}", file=sys.stderr)
                print(f"  Target branch '{new_target}' is already in use. Each target branch must be unique.", file=sys.stderr)
                sys.exit(1)


# ---------------------------------------------------------------------------
# Command: init
# ---------------------------------------------------------------------------


def cmd_init(
    config: Config,
    name: str | None = None,
    templates: list[str] | None = None,
    repos: dict[str, BranchSpec] | None = None,
    configuration: str | None = None,
) -> None:
    """Create a workspace in the current directory, or in ./NAME.

    Optional pre-populated values from CLI args:
      name: directory to create under the current one; without it, "here"
      templates: list of template names to apply
      repos: dict of alias -> BranchSpec
      configuration: path to an existing workspace config to duplicate
    """
    source_ws, ws_dir = _validate_init_inputs(config, name, templates, repos, configuration)

    # Read once, so the questionnaire and the confirmation cannot disagree.
    interactive = sys.stdin.isatty()

    if interactive:
        ws = _gather_workspace_config_interactive(config, source_ws, templates, repos)
    else:
        ws = _workspace_config_from_flags(config, source_ws, templates, repos)

    _check_duplicate_branches(ws.repos)

    print(f"\nWorkspace '{ws_dir.name}' will be created in {ws_dir} with:")
    print(f"  Templates: {', '.join(ws.templates)}")
    for alias, spec in ws.repos.items():
        print(f"  {alias}: {spec.to_spec_str()}")
    if ws.vars:
        print(f"  Vars: {ws.vars}")

    if interactive:
        try:
            confirm = questionary.confirm("Proceed?").ask()
        except KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if not confirm:
            print("Aborted.")
            return

    ow_config_path = ws_dir / MARKER
    ow_created_the_dir = not ws_dir.exists()
    ws_dir.mkdir(parents=True, exist_ok=True)

    _, successful, errors = ensure_workspace_materialized(ws, config, ws_dir)

    if errors:
        if len(errors) == len(ws.repos):
            if ow_created_the_dir:
                _cleanup_failed_workspace(ws_dir)
            print("\nError: all repos failed to set up:", file=sys.stderr)
            for alias, err in errors.items():
                print(f"  {alias}: {err}", file=sys.stderr)
            sys.exit(1)

        print("\nWarning: some repos failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    apply_templates(ws, config, ws_dir)

    write_workspace_config(ow_config_path, ws)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        run_cmd(["mise", "trust", str(mise_toml)], check=True)

    # The config file is the truth; the index only remembers where to find it.
    index.remember(ws_dir)

    if errors:
        print(f"\nWorkspace '{ws_dir.name}' created with errors. Fix issues and run: ow update")
    else:
        print(f"\nWorkspace '{ws_dir.name}' created. To install dependencies:")
        print(f"    cd {ws_dir} && mise install")
    print(f"\nWorkspace config: {ow_config_path}")
    print("Edit it to customize vars, then run: ow update")
