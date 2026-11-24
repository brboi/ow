"""`ow rm <ws|stack>`: remove a mono-stack workspace, a multi-stack workspace, or one stack."""

from __future__ import annotations

from pathlib import Path

from ow import term
from ow.commands._runtime import open_target, remove_workspace_root, running_refusals
from ow.core import git, runtime
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, OwStep, RepoPlan, Step, run_batch
from ow.core.plugins import Plugins
from ow.core.session import Session
from ow.recipes.removal import outside_entries, repo_removal_plan, workspace_refusals


def run(session: Session, *, dry_run: bool, yes: bool) -> int:
    """Remove a mono-stack workspace or a single stack; 1 when pre-flight refused."""
    workspace = session.workspace
    assert workspace is not None
    root = workspace.root
    facts = gather_all(session.locations)

    refusals = list(workspace_refusals(facts, root, session.locations))
    refusals.extend(running_refusals(workspace, session.plugins))
    if refusals:
        for name, text in refusals:
            term.out(f"# {name}: {text}")
        return 1

    outside = outside_entries(root, tuple(repo_facts.path for repo_facts in facts))
    if outside:
        term.out(f"# {outside}")

    outcome = run_batch(
        Batch(
            root=root,
            repos=tuple(repo_removal_plan(repo_facts) for repo_facts in facts),
            after=(_remove_step(workspace, session.plugins),),
        ),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    return outcome.exit_code


def run_multi(plugins: Plugins, multi_root: Path, *, dry_run: bool, yes: bool) -> int:
    """Remove every stack of multi-stack workspace `multi_root` in one batch; the last one takes the root with it."""
    target = open_target(multi_root, "multi", plugins)
    assert target.multi is not None
    multi = target.multi

    gathered: list[tuple[Session, tuple[RepoFacts, ...]]] = []
    any_refused = False
    for session in target.sessions:
        assert session.workspace is not None
        ws = session.workspace
        facts = gather_all(session.locations)
        refusals = list(workspace_refusals(facts, ws.root, session.locations))
        refusals.extend(running_refusals(ws, target.plugins))
        if refusals:
            any_refused = True
            for name, text in refusals:
                term.out(f"# {ws.name}/{name}: {text}")
        gathered.append((session, facts))

    # Also catch any root-level detached process tracked directly under `<multi.root>/.ow/run/`.
    stack_names = {ws.name for ws in target.workspaces}
    for proc in runtime.list_running(multi.root):
        if proc.label not in stack_names:
            any_refused = True
            term.out(f"# {multi.name}/{proc.label}: running (pid {proc.pid}); run `ow stop` first")

    if any_refused:
        return 1

    plans: list[RepoPlan] = []
    for session, facts in gathered:
        assert session.workspace is not None
        ws = session.workspace
        outside = outside_entries(ws.root, tuple(rf.path for rf in facts))
        if outside:
            term.out(f"# {ws.name}: {outside}")
        ws_steps: list[Step] = []
        ws_notes: list[str] = []
        for rf in facts:
            rp = repo_removal_plan(rf)
            ws_steps.extend(rp.steps)
            ws_notes.extend(rp.notes)
        ws_steps.append(_remove_step(ws, target.plugins))
        plans.append(
            RepoPlan(repo=f"stack:{ws.name}", steps=tuple(ws_steps), notes=tuple(ws_notes))
        )

    outcome = run_batch(
        Batch(root=None, repos=tuple(plans)),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    return outcome.exit_code


def _remove_step(ws, plugins: Plugins) -> OwStep:
    """The last step of a removal: delete the root and settle the index or the multi-stack root."""
    where = "from its multi-stack workspace" if ws.is_stack else "from the index"
    return OwStep(
        shown=f"{git.show(('rm', '-rf', str(ws.root)))}  # and drop it {where}",
        run=lambda: remove_workspace_root(ws, plugins),
    )
