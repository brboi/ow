"""Decision table tests for the `ow switch` lookup; pure, no git or filesystem access."""

from __future__ import annotations

from pathlib import Path

from ow.core.facts import RepoFacts
from ow.recipes.switch import Lookup, hint, lookup

BRANCH = "18.0-voip"


def _facts(remotes: tuple[str, ...] = ("origin",)) -> RepoFacts:
    """Facts for one repo; only `remotes` drives the lookup."""
    return RepoFacts(
        name="community",
        path=Path("/ws/community"),
        bare=Path("/repos/community.git"),
        remotes=remotes,
        head=None,
        branch=None,
        upstream=None,
        upstream_ab=None,
        base=None,
        default_base="origin/18.0",
        explicit_base=None,
        operation=None,
        modified=(),
        untracked=(),
        problem=None,
    )


def test_local_branch_wins_over_every_remote():
    """A local `refs/heads/<b>` is the local case, whatever the remotes hold."""
    result = lookup(
        _facts(("origin", "dev")),
        BRANCH,
        frozenset({BRANCH}),
        frozenset({f"origin/{BRANCH}", f"dev/{BRANCH}"}),
        {"origin"},
    )

    assert result == Lookup("local")


def test_cached_on_one_remote_tracks_it():
    """`<b>` cached on exactly one remote: create it tracking that remote."""
    result = lookup(_facts(), BRANCH, frozenset(), frozenset({f"origin/{BRANCH}"}), set())

    assert result == Lookup("track", "origin")


def test_cached_on_two_remotes_is_ambiguous():
    """`<b>` cached on two remotes: nothing is guessed, both are named."""
    result = lookup(
        _facts(("origin", "dev")),
        BRANCH,
        frozenset(),
        frozenset({f"origin/{BRANCH}", f"dev/{BRANCH}"}),
        set(),
    )

    assert result == Lookup("ambiguous", None, ("origin", "dev"))


def test_cached_holder_wins_over_a_live_one():
    """A cached branch is used without querying other remotes, so it is not ambiguous."""
    result = lookup(
        _facts(("origin", "dev")),
        BRANCH,
        frozenset(),
        frozenset({f"origin/{BRANCH}"}),
        {"dev"},
    )

    assert result == Lookup("track", "origin")


def test_ls_remote_hit_on_one_remote_fetches_first():
    """`<b>` on exactly one remote, nowhere cached: fetch, then track."""
    result = lookup(_facts(), BRANCH, frozenset(), frozenset(), {"origin"})

    assert result == Lookup("remote-fetch", "origin")


def test_ls_remote_hit_on_two_remotes_is_ambiguous():
    """`<b>` live on two remotes, nowhere cached: nothing is guessed."""
    result = lookup(
        _facts(("origin", "dev")), BRANCH, frozenset(), frozenset(), {"origin", "dev"}
    )

    assert result == Lookup("ambiguous", None, ("origin", "dev"))


def test_found_nowhere_is_none():
    """`<b>` neither local nor on any remote: nothing this repo can do."""
    result = lookup(_facts(), BRANCH, frozenset(), frozenset(), set())

    assert result == Lookup("none")


def test_a_stale_remote_tracking_ref_does_not_count():
    """A cached ref of a remote the repo no longer has is not a holder."""
    result = lookup(_facts(), BRANCH, frozenset(), frozenset({f"upstream/{BRANCH}"}), set())

    assert result == Lookup("none")


def test_create_falls_through_when_nothing_is_found():
    """`-c` runs the same lookup and falls through to `create` on nothing found."""
    result = lookup(_facts(), BRANCH, frozenset(), frozenset(), set(), create=True)

    assert result == Lookup("create")


def test_create_uses_a_found_branch_anyway():
    """`-c` with the branch already local is the local case, not a creation."""
    result = lookup(_facts(), BRANCH, frozenset({BRANCH}), frozenset(), set(), create=True)

    assert result == Lookup("local")


def test_hint_fetches_then_tracks_origin_first():
    """The ambiguity hint fetches first, then tracks, on the origin remote."""
    text = hint(_facts(), BRANCH, ("mirror", "origin"))

    assert text == (
        f"git -C /ws/community fetch origin +refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}; "
        f"git -C /ws/community switch -c {BRANCH} --no-track origin/{BRANCH}; "
        f"git -C /ws/community config branch.{BRANCH}.remote origin; "
        f"git -C /ws/community config branch.{BRANCH}.merge refs/heads/{BRANCH}"
    )


def test_hint_falls_back_to_the_first_remote():
    """Without origin, the hint names the first remote of the ambiguity."""
    text = hint(_facts(("mirror",)), BRANCH, ("mirror",), Path("/ws"))

    assert text == (
        f"git -C community fetch mirror +refs/heads/{BRANCH}:refs/remotes/mirror/{BRANCH}; "
        f"git -C community switch -c {BRANCH} --no-track mirror/{BRANCH}; "
        f"git -C community config branch.{BRANCH}.remote mirror; "
        f"git -C community config branch.{BRANCH}.merge refs/heads/{BRANCH}"
    )
