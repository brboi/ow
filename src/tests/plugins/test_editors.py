"""Tests for editors spec helpers."""

from __future__ import annotations

import pytest

from ow.core.fs import json_text
from ow.specs.editors import EditorContribution, merge


def test_json_text_two_space_indent_and_trailing_newline():
    """json_text emits two-space indented JSON with a trailing newline."""
    assert json_text({"a": 1}) == '{\n  "a": 1\n}\n'


def test_json_text_keeps_non_ascii_characters():
    """json_text does not escape non-ASCII characters."""
    assert "café" in json_text({"label": "café"})


def test_merge_rejects_non_mapping_language_server():
    """A language server whose value is not a mapping raises ValueError."""
    with pytest.raises(ValueError, match="language server 'odoo'"):
        merge([EditorContribution(language_servers={"odoo": "not-a-mapping"})])
