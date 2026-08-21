import subprocess
import sys

from ow.commands.templates import outdated_templates
from ow.utils.drift import warn_if_drifted
from ow.utils.config import Config
from ow.utils.git import run_cmd
from ow.utils.resolver import resolve_workspace
from ow.utils.templates import apply_templates, available_templates, ensure_services_compose, ensure_workspace_materialized, resolve_template_files


def cmd_apply(config: Config, workspace: str | None = None, *, check: bool = False) -> None:
    """Make the tree match .ow/config.toml: materialize worktrees, render templates."""
    ws_dir, ws = resolve_workspace(name=workspace)

    if check:
        # --check is read-only: it reports drift and stale templates
        # without materializing, rendering, or trusting anything. Exit
        # non-zero so a CI step or pre-flight script can gate on it.
        drifted = warn_if_drifted(ws, ws_dir)
        missing_worktrees = [
            alias for alias in ws.repos
            if not (ws_dir / alias).exists()
        ]
        if missing_worktrees:
            print(
                "Worktree(s) missing:",
                file=sys.stderr,
            )
            for alias in missing_worktrees:
                print(f"  {alias}", file=sys.stderr)
            drifted = True
        outdated = outdated_templates()
        if outdated:
            print("\nTemplate(s) ow has changed since you took them:")
            for name in outdated:
                print(f"  {name}")
        if drifted or outdated:
            sys.exit(1)
        print(f"\nWorkspace '{ws_dir.name}' is up to date.")
        return

    _, successful, errors = ensure_workspace_materialized(ws, config, ws_dir)
    ensure_services_compose()
    apply_templates(ws, config, ws_dir)

    # Report files that exist on disk but belong to template bundles not in
    # the workspace config.  Stateless: no manifest, no mutation.
    inactive_bundles = set(available_templates()) - set(ws.templates)
    orphans: list[tuple[str, str]] = []
    for bundle in sorted(inactive_bundles):
        try:
            files = resolve_template_files(bundle)
        except (OSError, FileNotFoundError):
            continue
        for rel, src in files.items():
            out_rel = rel.with_suffix("") if src.suffix == ".j2" else rel
            if (ws_dir / out_rel).exists():
                orphans.append((out_rel.as_posix(), bundle))
    if orphans:
        print(
            "\nSome files may come from templates not in your config. "
            "Remove them manually if stale:"
        )
        for file_path, bundle_name in orphans:
            print(f"  {file_path}  [{bundle_name}]")

    if errors:
        print("\nWarning: repo(s) failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        try:
            run_cmd(["mise", "trust", str(mise_toml)], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"\nWarning: could not trust {mise_toml}: {e}", file=sys.stderr)
            print(f"  Run it yourself when mise is happy: mise trust {mise_toml}", file=sys.stderr)

    outdated = outdated_templates()
    if outdated:
        print("\nTemplate(s) ow has changed since you took them:")
        for name in outdated:
            print(f"  {name}")
        print("Run `ow templates --diff` to see what changed.")

    if errors:
        # Everything above still ran — the templates are rendered — but a
        # workspace missing a repo is not applied, and a CI step that says
        # so must go red rather than green.
        noun = "repo" if len(errors) == 1 else "repos"
        print(f"\nWorkspace '{ws_dir.name}' partly applied: {len(errors)} {noun} failed.")
        sys.exit(1)

    print(f"\nWorkspace '{ws_dir.name}' applied.")
