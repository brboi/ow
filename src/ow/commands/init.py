"""Creating a workspace, here or in ./NAME.

Mirrors `git init`: a workspace is a directory holding a .ow/config.toml, and
its name is that directory's name — which is already what
`build_template_context` reads. Nothing here knows about a project root,
because there is none.
"""

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import questionary

from ow.utils import index
from ow.utils.templates import (
    apply_templates,
    available_templates,
    ensure_services_compose,
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
    available = available_templates()

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
        try:
            source_ws = load_workspace_config(src_config_file)
        except (OSError, ValueError) as exc:
            # TOMLDecodeError is a ValueError; a fumbled quote or missing
            # bracket deserves the same one-liner the global config gets,
            # not eight frames of tomllib.
            print(f"Error: could not load {src_config_file}: {exc}", file=sys.stderr)
            sys.exit(1)

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
        print(f"       {ws_dir / MARKER} exists; edit it and run `ow apply`.", file=sys.stderr)
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
    # Refuse only when nothing at all was given. A repo-less workspace is
    # legitimate here too — the interactive path already allows ticking no
    # repo, so `ow init tools -t common` in a script has to be allowed as
    # well. This guard exists solely to catch the accidental bare `ow init`
    # in a non-interactive context.
    if templates is None and repos is None and source_ws is None:
        avail_t = ", ".join(available_templates())
        avail_r = ", ".join(config.remotes) or "(none configured)"
        print("Error: stdin is not a terminal, so ow init cannot ask. Nothing was given:", file=sys.stderr)
        print(f"         -t/--template NAME     available: {avail_t}", file=sys.stderr)
        print(f"         -r/--repo ALIAS:SPEC   aliases: {avail_r}", file=sys.stderr)
        print("       or pass -c/--configuration to copy an existing workspace.", file=sys.stderr)
        sys.exit(1)

    chosen_templates, chosen_repos = _preselection(source_ws, templates, repos)

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
    available_t = available_templates()
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

    A workspace whose own config no longer reads is skipped for the same
    reason: nothing about the workspace being created is wrong, and git
    still refuses the second worktree if it turns out to collide.
    """
    for existing_ws_dir in index.known_workspaces():
        try:
            existing = load_workspace_config(existing_ws_dir / MARKER)
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            continue
        for alias, new_spec in new_repos.items():
            if alias not in existing.repos:
                continue
            existing_spec = existing.repos[alias]
            new_target = new_spec.local_branch
            existing_target = existing_spec.local_branch
            if new_target and existing_target and new_target == existing_target:
                print(f"Error: workspace '{existing_ws_dir.name}' already uses {alias}:{existing_spec.to_spec_str()}", file=sys.stderr)
                print(f"  Target branch '{new_target}' is already in use. Each target branch must be unique.", file=sys.stderr)
                print(f"  Use -r {alias}:SPEC to override the duplicated repo.", file=sys.stderr)
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
        # Labelled, and indented one step further than the repo lines above:
        # without that, a var named like a repo alias is indistinguishable
        # from one on the screen someone reads before typing `y`.
        print("  Vars:")
        for var_name, value in ws.vars.items():
            print(f"    {var_name}: {value}")

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

    write_workspace_config(ow_config_path, ws)

    # The config file is the truth; the index only remembers where to find it,
    # so it is written the moment the truth exists — nothing after this point
    # may cost the user a workspace that is already on disk.
    index.remember(ws_dir)

    ensure_services_compose()

    template_error = None
    try:
        apply_templates(ws, config, ws_dir)
    except Exception as exc:
        template_error = exc
        print(f"\nWarning: template rendering failed: {exc}", file=sys.stderr)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        try:
            run_cmd(["mise", "trust", str(mise_toml)], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"\nWarning: could not trust {mise_toml}: {e}", file=sys.stderr)
            print(f"  Run it yourself when mise is happy: mise trust {mise_toml}", file=sys.stderr)

    if errors or template_error:
        print(f"\nWorkspace '{ws_dir.name}' created with errors. Fix issues and run: ow apply")
    else:
        print(f"\nWorkspace '{ws_dir.name}' created. To install dependencies:")
        print(f"    cd {ws_dir} && mise install")
    print(f"\nWorkspace config: {ow_config_path}")
    print("Edit it to customize vars, then run: ow apply")

    # The workspace is complete and the user is told everything they would
    # have been told anyway; only the status code says a repo went wrong, so
    # a script that chains on `ow init` notices. Same rule as apply and rebase.
    if errors or template_error:
        sys.exit(1)
