"""Decision table tests for base branch parsing, inference, and followed ref."""

import dataclasses
from pathlib import Path

import pytest

from ow.core.bases import BaseError, is_upstream_base, parse_base, infer_default_base
from ow.core.facts import RepoFacts
from ow.recipes.follow import followed_ref


def make_facts(**changes) -> RepoFacts:
    """Build RepoFacts with sensible defaults, overriding the given fields."""
    base = RepoFacts(
        name="test",
        path=Path("/ws/test"),
        bare=Path("/data/repos/test.git"),
        remotes=("origin",),
        head=None,
        branch="main",
        upstream=None,
        upstream_ab=None,
        base="origin/main",
        default_base="origin/main",
        explicit_base=None,
        operation=None,
        modified=(),
        untracked=(),
        problem=None,
    )
    return dataclasses.replace(base, **changes)


class TestParseBase:
    """Test cases for parse_base() parsing and normalization."""

    @pytest.mark.parametrize(
        "ref,remotes,expected",
        [
            # Shorthands without known remote prefix -> origin/
            ("18.0", lambda r: r == "origin", "origin/18.0"),
            ("master", lambda r: r == "origin", "origin/master"),

            # Already fully qualified with known remote -> kept
            ("origin/master", lambda r: r == "origin", "origin/master"),

            # Fully qualified with another known remote -> kept
            ("dev/master-phoenix", lambda r: r in ("origin", "dev"), "dev/master-phoenix"),

            # Fully qualified with unknown remote -> prefix origin/
            ("dev/x", lambda r: r == "origin", "origin/dev/x"),

            # refs/remotes/<r>/<b> -> normalize to <r>/<b>
            ("refs/remotes/origin/18.0", lambda r: r == "origin", "origin/18.0"),

            # Slash-named remote: longest prefix wins
            (
                "gitlab-group/test/main",
                lambda r: r in ("origin", "gitlab-group/test"),
                "gitlab-group/test/main",
            ),
            # Slash-named remote prefix matched, branch keeps its own remote-ish prefix
            (
                "gitlab-group/test/sub/main",
                lambda r: r in ("origin", "gitlab-group/test"),
                "gitlab-group/test/sub/main",
            ),
            # Known remote name is a prefix but not a full segment -> no match
            (
                "gitlab-group/test/main",
                lambda r: r in ("origin", "gitlab"),
                "origin/gitlab-group/test/main",
            ),
        ],
    )
    def test_parse_base_valid(self, ref, remotes, expected):
        """Test valid base references are parsed correctly."""
        result = parse_base(ref, remotes)
        assert result == expected

    @pytest.mark.parametrize(
        "ref",
        [
            # Forbidden patterns
            "x..y",
            "a:b",
            "a^b",
            "a~1",

            # SHAs (7-40 hex chars)
            "abc1234",
            "abc123456789abc123456789abc123456789abc1",

            # Tags
            "refs/tags/v1",

            # Invalid branch refs
            "origin/",
            "",
            "-master",
            "origin master",
            "origin/master/",
            "origin/master.lock",

            # check-ref-format rules
            "a//b",
            "a/.b",
            "origin/a@{1}",
            "refs/remotes/origin",
            "origin/a.",
        ],
    )
    def test_parse_base_refused(self, ref):
        """Test that invalid bases are refused."""
        with pytest.raises(BaseError):
            parse_base(ref)


class TestIsUpstreamBase:
    """Test cases for is_upstream_base()."""

    @pytest.mark.parametrize(
        "upstream,base,expected",
        [
            # Both None -> False
            (None, None, False),

            # One is None -> False
            ("origin/18.0", None, False),
            (None, "origin/18.0", False),

            # Both set and equal -> True
            ("origin/18.0", "origin/18.0", True),

            # Both set but different -> False
            ("origin/18.0", "dev/18.0", False),
        ],
    )
    def test_is_upstream_base(self, upstream, base, expected):
        """Test upstream == base detection."""
        result = is_upstream_base(upstream, base)
        assert result is expected


class TestInferDefaultBase:
    """Test cases for infer_default_base()."""

    def test_agree_on_base_that_exists(self):
        """All repos agree and origin has the branch."""
        others = {"repo1": "origin/18.0", "repo2": "origin/18.0"}
        origin_has = lambda b: b in ("18.0",)
        result = infer_default_base(others, origin_has, None)

        assert result is not None
        base, source = result
        assert base == "origin/18.0"
        assert "shared by" in source
        assert "repo1" in source
        assert "repo2" in source

    def test_shared_non_origin_base_returns_origin(self):
        """Shared base on another remote maps to the new repo's origin."""
        others = {"enterprise": "dev/master-phoenix"}
        result = infer_default_base(others, lambda b: b == "master-phoenix", None)

        assert result is not None
        base, source = result
        assert base == "origin/master-phoenix"
        assert source == "shared by enterprise"

    def test_disagree_on_base(self):
        """Repos disagree on base -> fall back to origin/HEAD."""
        others = {"repo1": "origin/18.0", "repo2": "dev/18.0"}
        origin_has = lambda b: True
        result = infer_default_base(others, origin_has, "origin/18.0")

        assert result is not None
        base, source = result
        assert base == "origin/18.0"
        assert source == "origin/HEAD"

    def test_origin_lacks_agreed_branch(self):
        """All repos agree but origin doesn't have that branch -> fall back to origin/HEAD."""
        others = {"repo1": "origin/18.0", "repo2": "origin/18.0"}
        origin_has = lambda b: b != "18.0"  # origin doesn't have 18.0
        result = infer_default_base(others, origin_has, "origin/20.0")

        assert result is not None
        base, source = result
        assert base == "origin/20.0"
        assert source == "origin/HEAD"

    def test_no_origin_head_no_others(self):
        """No other repos and no origin/HEAD -> None."""
        others = {}
        origin_has = lambda b: True
        result = infer_default_base(others, origin_has, None)

        assert result is None

    def test_origin_head_without_prefix(self):
        """origin_head might not have 'origin/' prefix -> normalize it."""
        others = {}
        origin_has = lambda b: True
        result = infer_default_base(others, origin_has, "18.0")

        assert result is not None
        base, source = result
        assert base == "origin/18.0"
        assert source == "origin/HEAD"


class TestFollowedRef:
    """Test cases for followed_ref()."""

    def test_branch_with_upstream(self):
        """On a branch with an upstream: return the upstream."""
        facts = make_facts(upstream="origin/upstream-branch")
        assert followed_ref(facts) == "origin/upstream-branch"

    def test_branch_with_upstream_equal_to_base(self):
        """When upstream == base: treat as no upstream, return base."""
        facts = make_facts(upstream="origin/main", base="origin/main")
        assert followed_ref(facts) == "origin/main"

    def test_branch_with_upstream_and_no_base(self):
        """Upstream set, no base: upstream still wins."""
        facts = make_facts(upstream="origin/upstream-branch", base=None)
        assert followed_ref(facts) == "origin/upstream-branch"

    def test_branch_without_upstream(self):
        """On a branch without upstream: return the base."""
        facts = make_facts(branch="feature", base="origin/18.0")
        assert followed_ref(facts) == "origin/18.0"

    def test_detached_head(self):
        """On a detached HEAD: return the base."""
        facts = make_facts(head="abc123def456", branch=None, base="origin/18.0")
        assert followed_ref(facts) == "origin/18.0"

    def test_detached_head_without_any_base(self):
        """On a detached HEAD without any base: return None."""
        facts = make_facts(
            head="abc123def456",
            branch=None,
            base=None,
            default_base=None,
        )
        assert followed_ref(facts) is None
