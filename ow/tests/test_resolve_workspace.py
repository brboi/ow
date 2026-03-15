from __future__ import annotations

import os
import pytest

from ow.__main__ import resolve_workspace_name


class TestResolveWorkspaceName:
    def test_explicit_name_returned(self):
        assert resolve_workspace_name("foo") == "foo"

    def test_explicit_name_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OW_WORKSPACE", "bar")
        assert resolve_workspace_name("foo") == "foo"

    def test_env_var_used_when_no_name(self, monkeypatch):
        monkeypatch.setenv("OW_WORKSPACE", "bar")
        assert resolve_workspace_name(None) == "bar"

    def test_allow_all_returns_none_when_no_name_no_env(self, monkeypatch):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        assert resolve_workspace_name(None, allow_all=True) is None

    def test_allow_all_still_uses_env(self, monkeypatch):
        monkeypatch.setenv("OW_WORKSPACE", "bar")
        assert resolve_workspace_name(None, allow_all=True) == "bar"

    def test_exits_when_required_and_missing(self, monkeypatch):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        with pytest.raises(SystemExit):
            resolve_workspace_name(None)
