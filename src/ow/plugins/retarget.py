"""Opt-in `retarget` plugin: guarded multi-repo replay onto a base ref (`ow retarget`).

Disabled by default (`retarget.enabled = false`). Enable globally with:
    ow config set --global retarget.enabled true
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional

import typer

from ow import term
from ow.commands._shared import refresh_after, remotes_of
from ow.commands._sync import FETCH_FAILED, fetch_then_gather
from ow.core import bases, git
from ow.core.facts import RepoFacts
from ow.core.json_schema import envelope
from ow.core.plan import Batch, Outcome, RepoPlan, run_batch
from ow.core.plugins import Plugins, hookimpl
from ow.core.session import Session, open_session
from ow.errors import UsageError
from ow.recipes.preflight import refusal
from ow.recipes.rebase import gather_rebase, plan_is_fast_forward, plan_rebase

DEFAULT_ENABLED = False
TOP_LEVEL_COMMANDS = True


def execute(
    session: Session,
    *,
    only: Sequence[str],
    base: str | None,
    autostash: bool,
    no_fetch: bool,
    dry_run: bool,
    yes: bool,
) -> tuple[Outcome, int]:
    """Fetch, plan, prompt, run guarded replay, and refresh; returns `(outcome, exit_code)`."""
    new_base = _new_base(session, only, base)
    root, facts, failures = fetch_then_gather(
        session, only, no_fetch=no_fetch, extra=(new_base,) if new_base else ()
    )
    if not facts:
        return Outcome((), (), (), False, 0), 0

    plans = tuple(
        _plan(facts_, failures, new_base=new_base, autostash=autostash, root=root)
        for facts_ in facts
    )
    prompt = not all(
        plan_is_fast_forward(facts_, plan) for facts_, plan in zip(facts, plans)
    )
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=prompt, dry_run=dry_run, yes=yes
    )
    refreshed = refresh_after(session) if outcome.mutated else 0
    return outcome, max(outcome.exit_code, refreshed)


def run(
    session: Session,
    *,
    only: Sequence[str],
    base: str | None,
    autostash: bool,
    no_fetch: bool,
    dry_run: bool,
    yes: bool,
    json_output: bool = False,
) -> int:
    """CLI entrypoint for `ow retarget`."""
    with term.json_mode(json_output):
        outcome, code = execute(
            session,
            only=only,
            base=base,
            autostash=autostash,
            no_fetch=no_fetch,
            dry_run=dry_run,
            yes=yes,
        )
        if json_output:
            term.emit_json(
                envelope(
                    {
                        "ok": list(outcome.ok),
                        "failed": list(outcome.failed),
                        "refused": list(outcome.refused),
                        "mutated": outcome.mutated,
                        "dry_run": dry_run,
                        "exit_code": code,
                    }
                )
            )
        return code


def _new_base(session: Session, only: Sequence[str], base: str | None) -> str | None:
    """`--base` as a fully qualified ref, parsed against the selected repos' remotes."""
    if base is None:
        return None
    remotes = {
        remote
        for location in session.locations
        if not only or location.name in only
        for remote in remotes_of(location.name)
    } or {"origin"}
    return bases.parse_base(base, lambda segment: segment in remotes)


def _plan(
    facts: RepoFacts,
    failures: Mapping[str, object],
    *,
    new_base: str | None,
    autostash: bool,
    root: Path | None,
) -> RepoPlan:
    """One repo's plan: a fetch failure or pre-flight refusal, else the recipe's steps."""
    if failures.get(facts.name) is not None:
        return RepoPlan(facts.name, refusal=FETCH_FAILED)

    plan = plan_rebase(
        facts,
        gather_rebase(facts, new_base=new_base),
        new_base=new_base,
        autostash=autostash,
    )
    refused = refusal(
        facts,
        "retarget",
        autostash=autostash or not plan.steps,
        root=root,
        has_target_base=new_base is not None,
    )
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)
    return plan


@hookimpl
def ow_commands(app: typer.Typer, plugins: Plugins | None = None) -> None:
    """Register top-level `ow retarget` command when this plugin is enabled."""

    @app.command("retarget")
    def retarget_cmd(
        target_base: Optional[str] = typer.Argument(
            None,
            help="Optional new base ref to retarget onto (shorthand for --base)",
        ),
        base: Optional[str] = typer.Option(
            None,
            "--base",
            help="Rebase onto this base ref and record it as the branch's new base",
        ),
        autostash: bool = typer.Option(
            False, "--autostash", help="Stash modified files before rebasing and pop afterwards"
        ),
        only: Optional[list[str]] = typer.Option(
            None, "--only", help="Limit the command to this repo (repeatable; default: every repo)"
        ),
        no_fetch: bool = typer.Option(
            False, "--no-fetch", help="Do not fetch first; use the cached refs"
        ),
        dry_run: bool = typer.Option(
            False, "-n", "--dry-run", help="Print the git commands without running them"
        ),
        yes: bool = typer.Option(False, "-y", "--yes", help="Skip the confirmation prompt"),
        json_output: bool = typer.Option(
            False, "--json", help="Emit machine-readable JSON on stdout"
        ),
        workspace: Optional[str] = typer.Option(
            None,
            "-w",
            "--workspace",
            help="Workspace to act on, by name or path (default: current workspace)",
        ),
    ) -> None:
        """Guarded replay of each repo onto its base (or a new base ref)."""
        if target_base is not None and base is not None:
            raise UsageError("pass either positional BASE or --base, not both")
        effective_base = target_base if target_base is not None else base

        git.check_version()
        session = open_session(workspace, need_workspace=True, plugins=plugins)
        for warning in session.warnings:
            term.warn(warning)
        if not session.plugins.is_enabled("retarget", session.workspace):
            raise UsageError("retarget plugin is disabled for this workspace")
        if only:
            names = [loc.name for loc in session.locations]
            for repo in only:
                if repo not in names:
                    valid = ", ".join(names) if names else "(none)"
                    raise UsageError(
                        f"unknown repo {repo!r} in --only; this workspace has: {valid}"
                    )

        code = run(
            session,
            only=tuple(only or ()),
            base=effective_base,
            autostash=autostash,
            no_fetch=no_fetch,
            dry_run=dry_run,
            yes=yes,
            json_output=json_output,
        )
        raise typer.Exit(code)
