"""Tests for the bare `ow` invocation launching the dashboard."""
import sys
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner

from ow.__main__ import app


runner = CliRunner()


def test_no_args_non_tty_shows_help_and_exits_2(xdg):
    """ow without args in a non-TTY environment shows help and exits 2."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Odoo workspace manager" in result.output
def test_no_args_tty_launches_dashboard(xdg, monkeypatch):
    """ow without args in a TTY environment calls run_dashboard exactly once."""
    from ow.__main__ import callback
    import typer
    
    mock_run_dashboard = MagicMock()
    
    # Create a mock context
    ctx = typer.Context(typer.main.get_command(app))
    ctx.invoked_subcommand = None
    
    # Mock streams that report isatty() = True
    class MockTTYStream:
        def isatty(self):
            return True
    
    monkeypatch.setattr("sys.stdin", MockTTYStream())
    monkeypatch.setattr("sys.stdout", MockTTYStream())
    
    with patch("ow.tui.dashboard.run_dashboard", mock_run_dashboard):
        callback(ctx, version=False)
        
        mock_run_dashboard.assert_called_once()

def test_help_flag_works(xdg):
    """ow --help still shows help and exits 0."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Odoo workspace manager" in result.output


def test_subcommand_help_works(xdg):
    """ow status --help still shows subcommand help and exits 0."""
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    assert "Show workspace status" in result.output


def test_subcommand_does_not_launch_dashboard(xdg):
    """ow status does not call run_dashboard."""
    mock_run_dashboard = MagicMock()
    mock_cmd_status = MagicMock()
    
    with patch("ow.tui.dashboard.run_dashboard", mock_run_dashboard), \
         patch("ow.__main__.cmd_status", mock_cmd_status):
        result = runner.invoke(app, ["status"])
        
        mock_run_dashboard.assert_not_called()
        mock_cmd_status.assert_called_once()
