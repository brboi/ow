"""Decision table tests for the `ow switch` lookup (§6).

Pure tests with no git or filesystem access.
"""

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
    """A local `refs/heads/<b>` is case 1, whatever the remotes hold."""
    result = lookup(
        _facts(("origin", "dev")),
        BRANCH,
        frozenset({BRANCH, f"origin/{BRANCH}", f"dev/{BRANCH}"}),
        {"origin": ("abc refs/heads/18.0-voip",), "dev": ()},
    )

    assert result == Lookup("local")


def test_cached_on_one_remote_tracks_it():
    """`<b>` cached on exactly one remote: create it tracking that remote."""
    result = lookup(
        _facts(), BRANCH, frozenset({f"origin/{BRANCH}"}), {"origin": ()}
    )

    assert result == Lookup("track", "origin")


def test_cached_on_two_remotes_is_ambiguous():
    """`<b>` on two remotes: nothing is guessed, both are named."""
    result = lookup(
        _facts(("origin", "dev")),
        BRANCH,
        frozenset({f"origin/{BRANCH}", f"dev/{BRANCH}"}),
        {"origin": (), "dev": ()},
    )

    assert result == Lookup("none", None, ("origin", "dev"))


def test_ls_remote_hit_on_one_remote_fetches_first():
    """`<b>` on exactly one remote, nowhere cached: fetch, then track."""
    result = lookup(
        _facts(),
        BRANCH,
        frozenset(),
        {"origin": ("abc refs/heads/18.0-voip",)},
    )

    assert result == Lookup("remote-fetch", "origin")


def test_cached_and_published_on_two_remotes_is_ambiguous():
    """The two inputs count together: cached here, published there, is two remotes."""
    result = lookup(
        _facts(("origin", "dev")),
        BRANCH,
        frozenset({f"origin/{BRANCH}"}),
        {"origin": (), "dev": ("abc refs/heads/18.0-voip",)},
    )

    assert result == Lookup("none", None, ("origin", "dev"))


def test_cached_remote_with_a_ls_remote_hit_is_track():
    """A remote that already has `<b>` cached needs no fetch."""
    result = lookup(
        _facts(),
        BRANCH,
        frozenset({f"origin/{BRANCH}"}),
        {"origin": ("abc refs/heads/18.0-voip",)},
    )

    assert result == Lookup("track", "origin")


def test_found_nowhere_is_none():
    """`<b>` neither local nor on any remote: nothing this repo can do."""
    result = lookup(_facts(), BRANCH, frozenset(), {"origin": ()})

    assert result == Lookup("none")


def test_a_stale_remote_tracking_ref_does_not_count():
    """A cached ref of a remote the repo no longer has is not a holder."""
    result = lookup(_facts(), BRANCH, frozenset({f"upstream/{BRANCH}"}), {"origin": ()})

    assert result == Lookup("none")


def test_create_falls_through_when_nothing_is_found():
    """`-c` runs the same lookup and falls through to `create` on nothing found."""
    result = lookup(_facts(), BRANCH, frozenset(), {"origin": ()}, create=True)

    assert result == Lookup("create")


def test_create_uses_a_found_branch_anyway():
    """`-c` with the branch already local is the local case, not a creation."""
    result = lookup(_facts(), BRANCH, frozenset({BRANCH}), {"origin": ()}, create=True)

    assert result == Lookup("local")


def test_hint_names_the_by_hand_command():
    """The ambiguity hint is the exact `git -C <X> switch -c` line, origin first."""
    text = hint(_facts(), BRANCH, ("mirror", "origin"))

    assert text == f"git -C /ws/community switch -c {BRANCH} --track origin/{BRANCH}"

    text = hint(_facts(("mirror",)), BRANCH, ("mirror",))

    assert text == f"git -C /ws/community switch -c {BRANCH} --track mirror/{BRANCH}"
