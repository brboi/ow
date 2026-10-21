"""Decision table tests for base branch parsing and inference.

Pure tests with no git or filesystem access.
"""

import pytest

from ow.core.bases import BaseError, is_upstream_base, parse_base, infer_default_base


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
        ],
    )
    def test_parse_base_valid(self, ref, remotes, expected):
        """Test valid base references are parsed correctly."""
        result = parse_base(ref, remotes)
        assert result == expected

    @pytest.mark.parametrize(
        "ref,error_contains",
        [
            # Forbidden patterns - just check these patterns appear in the error message (case-insensitive)
            ("x..y", "contain"),
            ("a:b", "contain"),
            ("a^b", "contain"),
            ("a~1", "contain"),
            
            # SHAs (7-40 hex chars)
            ("abc1234", "sha"),
            ("abc123456789abc123456789abc123456789abc1", "sha"),
            
            # Tags
            ("refs/tags/v1", "tag"),
            
            # Empty branch or trailing
            ("origin/", "end"),
            
            # Empty string
            ("", "empty"),
            
            # Leading forbidden chars
            ("-master", "start"),
            ("@master", "start"),
            
            # Spaces
            ("origin master", "space"),
            
            # Trailing forbidden
            ("origin/master/", "end"),
            ("origin/master.lock", "end"),
        ],
    )
    def test_parse_base_refused(self, ref, error_contains):
        """Test that invalid bases are refused with appropriate messages."""
        with pytest.raises(BaseError) as exc_info:
            parse_base(ref)
        error_msg = str(exc_info.value).lower()
        assert error_contains.lower() in error_msg, \
            f"Expected '{error_contains}' in error message: {error_msg}"


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
        from ow.core.facts import RepoFacts
        from ow.recipes.follow import followed_ref

        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head=None,
            branch="main",
            upstream="origin/upstream-branch",
            upstream_ab=None,
            base="origin/main",
            default_base="origin/main",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert followed_ref(facts) == "origin/upstream-branch"

    def test_branch_with_upstream_equal_to_base(self):
        """When upstream == base: treat as no upstream, return base."""
        from ow.core.facts import RepoFacts
        from ow.recipes.follow import followed_ref

        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head=None,
            branch="main",
            upstream="origin/main",
            upstream_ab=None,
            base="origin/main",
            default_base="origin/main",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert followed_ref(facts) == "origin/main"

    def test_branch_without_upstream(self):
        """On a branch without upstream: return the base."""
        from ow.core.facts import RepoFacts
        from ow.recipes.follow import followed_ref

        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head=None,
            branch="feature",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base="origin/18.0",
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert followed_ref(facts) == "origin/18.0"

    def test_branch_without_base(self):
        """On a branch without explicit base: return default base."""
        from ow.core.facts import RepoFacts
        from ow.recipes.follow import followed_ref

        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head=None,
            branch="feature",
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

        # base is computed as explicit_base or default_base, so it should be None in the facts
        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head=None,
            branch="feature",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert followed_ref(facts) == "origin/18.0"

    def test_detached_head(self):
        """On a detached HEAD: return the default base."""
        from ow.core.facts import RepoFacts
        from ow.recipes.follow import followed_ref

        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head="abc123def456",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert followed_ref(facts) == "origin/18.0"

    def test_detached_head_without_default_base(self):
        """On a detached HEAD without default base: return None."""
        from ow.core.facts import RepoFacts
        from ow.recipes.follow import followed_ref

        facts = RepoFacts(
            name="test",
            path=None,
            bare=None,
            remotes=("origin",),
            head="abc123def456",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=None,
            default_base=None,
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert followed_ref(facts) is None


class TestFetchSteps:
    """Test cases for fetch_steps()."""

    def test_fetch_base_only(self):
        """Fetch the base branch from the correct remote."""
        from ow.core.facts import RepoFacts
        from ow.recipes.fetch import fetch_steps
        from pathlib import Path

        facts = RepoFacts(
            name="community",
            path=Path("/ws/community"),
            bare=Path("/data/repos/community.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        steps = fetch_steps(facts)
        assert len(steps) == 1
        assert steps[0].argv == ("git", "-C", "/data/repos/community.git", "fetch", "origin", "18.0")
        assert not steps[0].mutating

    def test_fetch_base_and_upstream(self):
        """Fetch both base and upstream from different remotes."""
        from ow.core.facts import RepoFacts
        from ow.recipes.fetch import fetch_steps
        from pathlib import Path

        facts = RepoFacts(
            name="enterprise",
            path=Path("/ws/enterprise"),
            bare=Path("/data/repos/enterprise.git"),
            remotes=("origin", "dev"),
            head=None,
            branch="main",
            upstream="dev/upstream-branch",
            upstream_ab=(1, 2),
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        steps = fetch_steps(facts)
        assert len(steps) == 2
        # First fetch from origin
        assert steps[0].argv == ("git", "-C", "/data/repos/enterprise.git", "fetch", "origin", "18.0")
        # Then fetch from dev
        assert steps[1].argv == ("git", "-C", "/data/repos/enterprise.git", "fetch", "dev", "upstream-branch")
        for step in steps:
            assert not step.mutating

    def test_fetch_upstream_equal_to_base(self):
        """When upstream equals base, fetch only once."""
        from ow.core.facts import RepoFacts
        from ow.recipes.fetch import fetch_steps
        from pathlib import Path

        facts = RepoFacts(
            name="community",
            path=Path("/ws/community"),
            bare=Path("/data/repos/community.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream="origin/18.0",
            upstream_ab=(0, 0),
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        steps = fetch_steps(facts)
        assert len(steps) == 1
        assert steps[0].argv == ("git", "-C", "/data/repos/community.git", "fetch", "origin", "18.0")

    def test_fetch_no_base_no_upstream(self):
        """No base and no upstream: no fetch needed."""
        from ow.core.facts import RepoFacts
        from ow.recipes.fetch import fetch_steps
        from pathlib import Path

        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=None,
            default_base=None,
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        steps = fetch_steps(facts)
        assert steps == ()

    def test_fetch_remote_with_slash_in_name(self):
        """Remote names can contain slashes: use the longest match."""
        from ow.core.facts import RepoFacts
        from ow.recipes.fetch import fetch_steps
        from pathlib import Path

        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin", "gitlab-group/test"),
            head=None,
            branch="feature",
            upstream=None,
            upstream_ab=None,
            base="gitlab-group/test/main",
            default_base="gitlab-group/test/main",
            explicit_base="gitlab-group/test/main",
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        steps = fetch_steps(facts)
        assert len(steps) == 1
        assert steps[0].argv == ("git", "-C", "/data/repos/test.git", "fetch", "gitlab-group/test", "main")

    def test_fetch_problem_repo(self):
        """A problem repo should not generate fetch steps."""
        from ow.core.facts import RepoFacts
        from ow.recipes.fetch import fetch_steps
        from pathlib import Path

        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch=None,
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem="prunable",
        )

        steps = fetch_steps(facts)
        assert steps == ()
