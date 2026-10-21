"""Tests for editors spec and merging."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from ow.specs.editors import (
    DebugLaunch,
    EditorContribution,
    merge as merge_editors,
)


@dataclass(frozen=True)
class FakeWorkspace:
    """Minimal workspace for testing."""

    root: Path
    name: str
    settings: Mapping[str, object]


def test_merge_empty():
    """Test merging empty contributions."""
    result = merge_editors([])
    assert result.launches == ()
    assert result.language_servers == {}
    assert result.excludes == ()


def test_merge_single_contribution():
    """Test merging a single contribution."""
    contrib = EditorContribution(
        launches=(
            DebugLaunch(
                name="Test",
                program=Path("/root/prog"),
                args=("arg1",),
                cwd=Path("/root"),
                env={"KEY": "value"},
            ),
        ),
        language_servers={"python": {"setting": "value"}},
        excludes=("**/*.po",),
    )
    result = merge_editors([contrib])
    assert len(result.launches) == 1
    assert result.launches[0].name == "Test"
    assert result.language_servers == {"python": {"setting": "value"}}
    assert result.excludes == ("**/*.po",)


def test_merge_multiple_launches():
    """Test merging multiple launches from different contributions."""
    launch1 = DebugLaunch(
        name="Launch1",
        program=Path("/root/p1"),
        args=(),
        cwd=Path("/root"),
        env={},
    )
    launch2 = DebugLaunch(
        name="Launch2",
        program=Path("/root/p2"),
        args=(),
        cwd=Path("/root"),
        env={},
    )
    contrib1 = EditorContribution(launches=(launch1,))
    contrib2 = EditorContribution(launches=(launch2,))
    
    result = merge_editors([contrib1, contrib2])
    assert len(result.launches) == 2
    assert result.launches[0].name == "Launch1"
    assert result.launches[1].name == "Launch2"


def test_merge_excludes_deduplication():
    """Test that excludes are deduplicated while preserving order."""
    contrib1 = EditorContribution(excludes=("**/*.po", "**/*.pot", "**/node_modules"))
    contrib2 = EditorContribution(excludes=("**/*.po", "**/dist"))
    
    result = merge_editors([contrib1, contrib2])
    # Should deduplicate while keeping order
    assert result.excludes == ("**/*.po", "**/*.pot", "**/node_modules", "**/dist")


def test_merge_language_servers_per_key():
    """Test that language servers are merged per server id and per key."""
    contrib1 = EditorContribution(
        language_servers={
            "python": {"key1": "value1", "key2": "value2"},
            "javascript": {"key1": "value1"},
        }
    )
    contrib2 = EditorContribution(
        language_servers={
            "python": {"key2": "updated", "key3": "value3"},
            "rust": {"key1": "value1"},
        }
    )
    
    result = merge_editors([contrib1, contrib2])
    assert result.language_servers == {
        "python": {"key1": "value1", "key2": "updated", "key3": "value3"},
        "javascript": {"key1": "value1"},
        "rust": {"key1": "value1"},
    }


def test_merge_with_none_answers():
    """Test merging with None answers mixed in."""
    contrib = EditorContribution(launches=(
        DebugLaunch(
            name="Test",
            program=Path("/root/prog"),
            args=(),
            cwd=Path("/root"),
            env={},
        ),
    ))
    
    result = merge_editors([None, contrib, None])
    assert len(result.launches) == 1
    assert result.launches[0].name == "Test"


def test_vscode_json_serialization(tmp_path):
    """Test that VSCode generates valid JSON files."""
    from ow.plugins.vscode import json_text
    
    data = {"key": "value", "nested": {"inner": True}}
    result = json_text(data)
    
    # Should have trailing newline
    assert result.endswith("\n")
    
    # Should parse as valid JSON
    parsed = json.loads(result)
    assert parsed == data
    
    # Should have proper indentation (2 spaces)
    assert '  ' in result


def test_zed_json_serialization(tmp_path):
    """Test that Zed generates valid JSON files."""
    from ow.plugins.zed import json_text
    
    data = {"key": "value", "array": [1, 2, 3]}
    result = json_text(data)
    
    # Should have trailing newline
    assert result.endswith("\n")
    
    # Should parse as valid JSON
    parsed = json.loads(result)
    assert parsed == data
    
    # Should have proper indentation (2 spaces)
    assert '  ' in result
