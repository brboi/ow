"""Print each repo's table in columns (TTY) or tab-separated (piped).

One tab-separated line per repo when piped; when stdout is a TTY, aligned columns with header.
The fields are: name, state, base, upstream, base counts, dirty, operation.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from ow import term
from ow.core import git
from ow.core.facts import RepoFacts, gather_all
from ow.core.session import Session


def run(session: Session) -> int:
    """Print every repo's table line; always 0."""
    print_table(session, gather_all(session.locations))
    return 0


def print_table(session: Session, facts: Sequence[RepoFacts]) -> None:
    """Format and print table: aligned columns (TTY) or tab-separated (piped)."""
    # Compute base_counts in parallel for all repos
    base_counts_cache = _compute_base_counts_parallel(facts)
    
    rows = [_row(f, base_counts_cache, i) for i, f in enumerate(facts)]
    
    if sys.stdout.isatty():
        # Aligned columns with header for terminal output
        headers = ["name", "state", "base", "upstream", "base counts", "dirty", "operation"]
        _print_aligned(headers, rows)
    else:
        # Tab-separated for piped output (machine-friendly)
        for row in rows:
            _line(row)
    
    # Print diagnostic messages
    for plugin, diagnostic in session.diagnostics:
        if diagnostic.level == "info":
            term.out(f"# {plugin}: {diagnostic.text}")


def _compute_base_counts_parallel(facts: Sequence[RepoFacts]) -> dict[int, str]:
    """Compute base counts for all repos in parallel using ThreadPoolExecutor."""
    cache = {}
    if not facts:
        return cache
    
    with ThreadPoolExecutor() as executor:
        # Map each repo to its base_counts computation
        futures = {executor.submit(_compute_base_counts_for_fact, fact): i for i, fact in enumerate(facts)}
        for future in futures:
            i = futures[future]
            cache[i] = future.result()
    
    return cache


def _compute_base_counts_for_fact(facts: RepoFacts) -> str:
    """`ahead X, behind Y` of HEAD vs base; `(even)` at 0/0; `no base` when none; `(?)` on error."""
    if facts.base is None:
        return "no base"
    if facts.problem:
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
        behind, ahead = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return "(?)"
    if ahead == 0 and behind == 0:
        return "(even)"
    return f"ahead {ahead}, behind {behind}"


def _print_aligned(headers: Sequence[str], rows: Sequence[str]) -> None:
    """Print aligned columns for TTY output."""
    if not rows:
        return
    
    # Parse each row into fields
    parsed_rows = [row.split("\t") for row in rows]
    
    # Calculate column widths: max of header width and data widths
    col_widths = []
    for i in range(len(headers)):
        max_width = len(headers[i])
        for parsed_row in parsed_rows:
            if i < len(parsed_row):
                max_width = max(max_width, len(parsed_row[i]))
        col_widths.append(max_width)
    
    # Print header with padding
    header_line = "  ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers)))
    _line(header_line)
    
    # Print data rows with padding
    for parsed_row in parsed_rows:
        padded_fields = []
        for i, field in enumerate(parsed_row):
            if i < len(col_widths) - 1:
                padded_fields.append(field.ljust(col_widths[i]))
            else:
                # Last column needs no padding
                padded_fields.append(field)
        _line("  ".join(padded_fields))


def _row(facts: RepoFacts, base_counts_cache: dict[int, str], index: int) -> str:
    """One repo's fields, tab-separated."""
    fields = [
        facts.name,
        _state(facts),
        _base(facts),
        _upstream(facts),
        base_counts_cache.get(index, "(??)"),
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
    """The base branch (origin/18.0), `(default)` when fallback, or `no base` when none."""
    if facts.base is None:
        return "no base"
    if facts.explicit_base is None and facts.default_base is not None:
        return f"{facts.base} (default)"
    return facts.base


def _upstream(facts: RepoFacts) -> str:
    """The upstream with its counts; `(none)` when absent or equal to base; bare ref when no counts."""
    upstream = facts.upstream
    if upstream is None or upstream == facts.base:
        return "(none)"
    if facts.upstream_ab is None:
        return upstream
    ahead, behind = facts.upstream_ab
    return f"{upstream} (ahead {ahead}, behind {behind})"


def _line(text: str) -> None:
    """Write one tab-separated line to stdout verbatim."""
    sys.stdout.write(f"{text}\n")
