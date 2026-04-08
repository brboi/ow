import time

from ow.utils.display import Spinner


class TestSpinner:
    def test_spinner_context_manager(self, capsys):
        with Spinner("Testing"):
            pass
        captured = capsys.readouterr()
        assert "\r" in captured.out
        assert "Testing" in captured.out

    def test_spinner_clears_on_exit(self, capsys):
        with Spinner("Prefix"):
            pass
        captured = capsys.readouterr()
        assert captured.out.endswith("\r")

    def test_spinner_animates(self, capsys):
        with Spinner("Anim"):
            time.sleep(0.25)
        captured = capsys.readouterr()
        assert captured.out.count("\r") >= 2
