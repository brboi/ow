"""Tests for editors spec helpers."""

from __future__ import annotations

import pytest

from ow.core.plugins import PluginError
from ow.specs.editors import EditorContribution, json_text, merge


def test_json_text_two_space_indent_and_trailing_newline():
    """json_text emits two-space indented JSON with a trailing newline."""
    assert json_text({"a": 1}) == '{\n  "a": 1\n}\n'


def test_json_text_keeps_non_ascii_characters():
    """json_text does not escape non-ASCII characters."""
    assert "café" in json_text({"label": "café"})


def test_merge_rejects_non_mapping_language_server():
    """A language server whose value is not a mapping raises PluginError."""
    with pytest.raises(PluginError) as exc_info:
        merge([EditorContribution(language_servers={"odoo": "not-a-mapping"})])

    assert exc_info.value.plugin == "odoo"
    assert exc_info.value.hook == "editors_contribution"
