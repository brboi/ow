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
