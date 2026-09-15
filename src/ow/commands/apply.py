import subprocess
import sys

from ow.utils.drift import warn_if_drifted
from ow.utils.config import Config
from ow.utils.git import run_cmd
from ow.utils.resolver import resolve_workspace
from ow.utils.templates import (
    ABSENT,
    OUTDATED,
    YOURS,
    apply_templates,
    ensure_services_compose,
    ensure_workspace_materialized,
    legacy_mise_toml,
    rendered_states,
)


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

        states = rendered_states(ws, config, ws_dir)
        stale = [s for s in states if s.state in (OUTDATED, ABSENT)]
        yours = [s for s in states if s.state == YOURS]

        if stale:
            print("\ntemplate(s) ow would write:")
            for s in stale:
                print(f"  {s.path}  {s.state}")
        if yours:
            print("\nyours, left alone: " + ", ".join(s.path for s in yours))

        if drifted or stale:
            sys.exit(1)
        print(f"\nWorkspace '{ws_dir.name}' is up to date.")
        return

    _, successful, errors = ensure_workspace_materialized(ws, config, ws_dir)
    ensure_services_compose()
    result = apply_templates(ws, config, ws_dir)

    for path in result.wrote:
        print(f"wrote {path}")
    for path in result.updated:
        print(f"updated {path}")
    if result.yours:
        print("yours, left alone: " + ", ".join(result.yours))
        print("run `ow templates --diff` to see what ow would write instead.")
    if result.skipped:
        print("not rendered (empty): " + ", ".join(result.skipped))

    legacy = legacy_mise_toml(ws_dir)
    if legacy is not None:
        print(
            f"warning: {legacy} was written by an older ow and overrides mise/conf.d/00-ow.toml",
            file=sys.stderr,
        )
        print("         delete it, or move what you want to keep into mise.local.toml", file=sys.stderr)

    if errors:
        print("\nWarning: repo(s) failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    # The fragments ow renders under mise/ need trusting, and `managed` names
    # them without rendering the workspace a second time.
    mise_fragments = [
        ws_dir / path for path in result.managed if path.startswith("mise/")
    ]
    for mise_toml in mise_fragments:
        try:
            run_cmd(["mise", "trust", str(mise_toml)], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"\nWarning: could not trust {mise_toml}: {e}", file=sys.stderr)
            print(f"  Run it yourself when mise is happy: mise trust {mise_toml}", file=sys.stderr)

    if errors:
        # Everything above still ran — the templates are rendered — but a
        # workspace missing a repo is not applied, and a CI step that says
        # so must go red rather than green.
        noun = "repo" if len(errors) == 1 else "repos"
        print(f"\nWorkspace '{ws_dir.name}' partly applied: {len(errors)} {noun} failed.")
        sys.exit(1)

    print(f"\nWorkspace '{ws_dir.name}' applied.")
