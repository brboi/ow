"""`ow status` — the workspace table, from git reads only (§7).

It never touches the network, never refreshes and never scans files. The facts come
from `ow.core.facts` (the branch, `@{u}`, ahead/behind `@{u}`, the dirty entries and
any operation in progress), plus one `rev-list --left-right --count <base>...HEAD` per
repo for the counts against the base. `ow_diagnostics` already ran when the session
opened; its `info` texts are printed after the table, and a plugin that raised is a
session warning, never a traceback (P13).

One tab-separated line per repo, in workspace order. The lines go to stdout with
`sys.stdout.write`, because Rich expands every tab to the next tab stop.

The fields, in order:

    <name>       the repo name
    <state>      the branch (`18.0-voip`), `detached at <sha10>` (or plain `detached`
                 when git reports no commit yet), or `(<problem> worktree)` for a
                 worktree ow cannot read (`run ow init`)
    <base>       the branch's base: `origin/18.0`, `origin/18.0 (default)` when it fell
                 back to the repo's default base because no `branch.<b>.ow-base` exists,
                 or `(unset)` when the repo has neither
    <upstream>   `dev/18.0-voip (ahead 1, behind 2)`, the bare ref when git reports no
                 counts for it, or `(none)` when the branch has no upstream — an
                 upstream equal to the base counts as none (§5)
    <base ab>    `ahead 1, behind 0` against the base, `(even)` at 0/0, `(unset)` when
                 there is no base, `(?)` when the count cannot be read
    <dirty>      `dirty 3`: the changed and untracked entries; the field is omitted
                 when the tree is clean
    <operation>  `<operation> in progress`, with git's own name — rebase, cherry-pick,
                 merge, revert or bisect; the field is omitted when none is in progress.
                 When the tree is clean and one is, the dirty field is written empty so
                 the operation always lands in the last field.
"""

from __future__ import annotations

import sys
from typing import Sequence

from ow import term
from ow.core import git
from ow.core.facts import RepoFacts, gather_all
from ow.core.session import Session
from ow.errors import UsageError


def run(session: Session) -> int:
    """Print every repo's table line; always 0 — status refuses nothing."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    print_table(session, gather_all(session.locations))
    return 0


def print_table(session: Session, facts: Sequence[RepoFacts]) -> None:
    """One table line per fact, then one `# <plugin>: <text>` line per info diagnostic."""
    for repo_facts in facts:
        _line(_row(repo_facts))
    for plugin, diagnostic in session.diagnostics:
        if diagnostic.level == "info":
            term.out(f"# {plugin}: {diagnostic.text}")


def _row(facts: RepoFacts) -> str:
    """One repo's fields, tab-separated."""
    fields = [
        facts.name,
        _state(facts),
        _base(facts),
        _upstream(facts),
        _base_counts(facts),
    ]
    dirty = len(facts.modified) + len(facts.untracked)
    if dirty:
        fields.append(f"dirty {dirty}")
    elif facts.operation:
        # Keep the operation in the last field even when there is nothing to report.
        fields.append("")
    if facts.operation:
        fields.append(f"{facts.operation} in progress")
    return "\t".join(fields)


def _state(facts: RepoFacts) -> str:
    """The branch, the detached commit, or the worktree problem that stops the reads."""
    if facts.problem:
        return f"({facts.problem} worktree)"
    if facts.branch:
        return facts.branch
    if facts.head:
        return f"detached at {facts.head[:10]}"
    return "detached"


def _base(facts: RepoFacts) -> str:
    """The base, marked `(default)` when it is the repo's default rather than `ow-base`."""
    if facts.base is None:
        return "(unset)"
    if facts.explicit_base is None and facts.default_base is not None:
        return f"{facts.base} (default)"
    return facts.base


def _upstream(facts: RepoFacts) -> str:
    """The upstream with its counts; `(none)` when there is none, or it is the base."""
    upstream = facts.upstream
    if upstream is None or upstream == facts.base:
        return "(none)"
    if facts.upstream_ab is None:
        return upstream
    ahead, behind = facts.upstream_ab
    return f"{upstream} (ahead {ahead}, behind {behind})"


def _base_counts(facts: RepoFacts) -> str:
    """`ahead X, behind Y` of HEAD against the base, read from `rev-list --left-right`."""
    if facts.base is None:
        return "(unset)"
    if facts.problem:
        # A broken worktree has no HEAD to count; do not spawn git at a path that is gone.
        return "(?)"

    result = git.run(
        [
            "git",
            "-C",
            str(facts.path),
            "rev-list",
            "--left-right",
            "--count",
            f"{facts.base}...HEAD",
        ]
    )
    if result.returncode != 0:
        return "(?)"

    parts = result.stdout.split()
    try:
        # `--left-right` prints the left side (base-only) first: behind, then ahead.
        behind, ahead = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return "(?)"
    if ahead == 0 and behind == 0:
        return "(even)"
    return f"ahead {ahead}, behind {behind}"


def _line(text: str) -> None:
    """Write one tab-separated line to stdout verbatim."""
    sys.stdout.write(f"{text}\n")