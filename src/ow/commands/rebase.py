"""`ow rebase`: guarded multi-repo update onto each repo's base, or a series change onto a new base ref."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ow.commands._shared import print_dropped
from ow.core import bases, config, git
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, Outcome, RepoPlan, run_batch
from ow.core.refresh import refresh_after
from ow.core.session import Session
from ow.recipes.fetch import FETCH_FAILED, fetch_then_gather
from ow.recipes.preflight import refusal
from ow.recipes.rebase import gather_rebase, plan_is_fast_forward, plan_rebase


def _split_base(ref: str, repo: str) -> tuple[str, str]:
    """`ref` as `(remote, branch)` against `repo`'s remotes."""
    remotes = set(git.remotes_of(repo)) or {"origin"}
    return bases.split_base(ref, lambda segment: segment in remotes)


def _repo_has_target_base(bare: Path, bare_name: str, new_base: str, *, no_fetch: bool) -> bool:
    """Whether `bare` has `new_base` on its target remote (or locally cached when `no_fetch`)."""
    remote, branch = _split_base(new_base, bare_name)
    if remote not in git.remotes(bare):
        return False
    if no_fetch:
        return git.has_ref(bare, f"refs/remotes/{remote}/{branch}")
    try:
        return git.remote_has_branch(bare, remote, branch)
    except git.GitFailed:
        # Network/auth error will be surfaced normally by the fetch step.
        return True


def _update_default_bases(session: Session, updated_repos: Mapping[str, str]) -> bool:
    """Write updated `[repos]` default bases into `.ow/config.toml`; returns True if changed."""
    if not updated_repos or session.config_path is None:
        return False
    merged = dict(session.accepted_bases)
    changed = False
    for name, base_ref in updated_repos.items():
        if merged.get(name) != base_ref:
            merged[name] = base_ref
            changed = True
    if not changed:
        return False
    if session.stack_name is not None:
        dropped = config.update_stack(
            session.config_path,
            session.stack_name,
            merged,
            lambda current: current,
            declared=session.declared,
            remotes_of=git.remotes_of,
        )
    else:
        dropped = config.update_workspace(
            session.config_path,
            merged,
            lambda current: current,
            declared=session.declared,
            remotes_of=git.remotes_of,
        )
    print_dropped(dropped)
    return True


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
    """Fetch, plan, prompt, run guarded replay, update `[repos]` bases, and refresh."""
    new_base = _new_base(session, only, base)
    selected_locations = tuple(
        loc for loc in session.locations if not only or loc.name in only
    )
    if not selected_locations:
        return Outcome((), (), (), False, 0), 0

    skipped_missing: dict[str, str] = {}
    target_names: list[str] = []
    if new_base is not None:
        for loc in selected_locations:
            if loc.problem:
                target_names.append(loc.name)
                continue
            if not _repo_has_target_base(loc.bare, loc.name, new_base, no_fetch=no_fetch):
                remote, branch = _split_base(new_base, loc.name)
                skipped_missing[loc.name] = f"remote {remote} has no branch {branch}; skipping"
            else:
                target_names.append(loc.name)
    else:
        target_names = [loc.name for loc in selected_locations]

    assert session.workspace is not None
    root = session.workspace.root
    if target_names:
        root, fetched_facts, failures = fetch_then_gather(
            session,
            tuple(target_names),
            no_fetch=no_fetch,
            extra=(new_base,) if new_base else (),
        )
    else:
        fetched_facts = ()
        failures = {}

    facts_by_name = {f.name: f for f in fetched_facts}
    skipped_locations = tuple(loc for loc in selected_locations if loc.name in skipped_missing)
    if skipped_locations:
        for f in gather_all(skipped_locations):
            facts_by_name[f.name] = f

    ordered_facts = tuple(facts_by_name[loc.name] for loc in selected_locations if loc.name in facts_by_name)
    plans_list: list[RepoPlan] = []
    for facts_ in ordered_facts:
        if facts_.name in skipped_missing:
            plans_list.append(RepoPlan(facts_.name, notes=(skipped_missing[facts_.name],)))
        else:
            plans_list.append(
                _plan(facts_, failures, new_base=new_base, autostash=autostash, root=root)
            )
    plans = tuple(plans_list)

    prompt = not all(
        plan_is_fast_forward(facts_, plan) for facts_, plan in zip(ordered_facts, plans)
    )
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=prompt, dry_run=dry_run, yes=yes
    )

    config_mutated = False
    if new_base is not None and not dry_run:
        to_record: dict[str, str] = {}
        ok_set = set(outcome.ok)
        for facts_ in ordered_facts:
            if facts_.name in skipped_missing or facts_.name not in ok_set:
                continue
            if facts_.base != new_base or session.accepted_bases.get(facts_.name) != new_base:
                to_record[facts_.name] = new_base
        if to_record:
            config_mutated = _update_default_bases(session, to_record)

    mutated = outcome.mutated or config_mutated
    refreshed = refresh_after(session) if mutated else 0
    final_outcome = Outcome(
        outcome.ok,
        outcome.failed,
        outcome.refused,
        mutated,
        max(outcome.exit_code, refreshed),
        outcome.after_failed,
    )
    return final_outcome, final_outcome.exit_code


def run(
    session: Session,
    *,
    only: Sequence[str],
    base: str | None,
    autostash: bool,
    no_fetch: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """CLI entrypoint for `ow rebase`."""
    _outcome, code = execute(
        session,
        only=only,
        base=base,
        autostash=autostash,
        no_fetch=no_fetch,
        dry_run=dry_run,
        yes=yes,
    )
    return code


def _new_base(session: Session, only: Sequence[str], base: str | None) -> str | None:
    """The BASE argument as a fully qualified ref, parsed against the selected repos' remotes."""
    if base is None:
        return None
    remotes = {
        remote
        for location in session.locations
        if not only or location.name in only
        for remote in git.remotes_of(location.name)
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
        "rebase",
        autostash=autostash or not plan.steps,
        root=root,
        has_target_base=new_base is not None,
    )
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)
    return plan
