"""Decision table for the §7 "pushed" rule: a branch tip contained in some refs/remotes/*.

Pure: `contains` is injected, so the table needs no git.
"""

from __future__ import annotations

import pytest

from ow.recipes.removal import is_pushed


def _contains(pairs):
    """A `contains` built from explicit (ancestor, descendant) pairs; equality included."""
    edges = set(pairs)
    return lambda ancestor, descendant: (ancestor, descendant) in edges


@pytest.mark.parametrize(
    "tip,remote_tips,pairs,expected",
    [
        # Equal to the only remote tip: a branch just created from its base.
        ("T", ["T"], [("T", "T")], True),
        # An ancestor of the only remote tip.
        ("T", ["R"], [("T", "R")], True),
        # An ancestor of none: the containment runs the other way, so the
        # argument order is part of what this row pins down.
        ("T", ["R"], [("R", "T")], False),
        # Multiple remote tips: contained in the second.
        ("T", ["R1", "R2"], [("T", "R2")], True),
        # Multiple remote tips: contained in none of them.
        ("T", ["R1", "R2"], [("R1", "R2"), ("R2", "R1")], False),
        # No remote tip at all: refusing to delete is the safe direction.
        ("T", [], [("T", "T")], False),
    ],
)
def test_is_pushed_table(tip, remote_tips, pairs, expected):
    """The §7 rule over the injected `contains`: some remote tip contains the branch tip."""
    assert is_pushed(tip, remote_tips, _contains(pairs)) is expected
