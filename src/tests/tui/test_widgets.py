import asyncio
import pytest
from ow.tui.widgets import LabeledInput, ConfirmDialog


def test_labeled_input_initial_value():
    from textual.app import App
    from textual.widgets import Input

    class TestApp(App):
        def compose(self):
            yield LabeledInput("spec", value="master")

    async def run_test():
        async with TestApp().run_test() as pilot:
            li = pilot.app.query_one(LabeledInput)
            inp = li.query_one(Input)
            return inp.value

    result = asyncio.run(run_test())
    assert result == "master"


def test_labeled_input_disabled_not_focusable():
    """A disabled LabeledInput's Input is not focusable."""
    from textual.app import App
    from textual.widgets import Input

    class TestApp(App):
        def compose(self):
            yield LabeledInput("spec", value="master", enabled=False)

    async def run_test():
        async with TestApp().run_test() as pilot:
            li = pilot.app.query_one(LabeledInput)
            inp = li.query_one(Input)
            assert inp.disabled is True

    asyncio.run(run_test())


def test_labeled_input_set_enabled():
    """set_enabled(True) re-enables the input."""
    from textual.app import App
    from textual.widgets import Input

    class TestApp(App):
        def compose(self):
            yield LabeledInput("spec", value="master", enabled=False)

    async def run_test():
        async with TestApp().run_test() as pilot:
            li = pilot.app.query_one(LabeledInput)
            li.set_enabled(True)
            inp = li.query_one(Input)
            assert inp.disabled is False

    asyncio.run(run_test())


def test_labeled_input_accepts_kwargs():
    """LabeledInput forwards **kwargs to Horizontal (R1 ruling)."""
    from textual.app import App

    class TestApp(App):
        def compose(self):
            yield LabeledInput("spec", value="main", id="li_test")

    async def run_test():
        async with TestApp().run_test() as pilot:
            li = pilot.app.query_one("#li_test", LabeledInput)
            assert li.id == "li_test"

    asyncio.run(run_test())


def test_confirm_dialog_yes_returns_true():
    """Yes button dismisses with True."""
    from textual.app import App
    from textual.widgets import Button

    class HostApp(App):
        def __init__(self):
            super().__init__()
            self.dialog_result = None

        async def on_mount(self):
            self.push_screen(ConfirmDialog("Delete workspace?"), callback=self._on_dialog)

        def _on_dialog(self, result: bool):
            self.dialog_result = result

    async def run_test():
        app = HostApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Query the current screen (the modal dialog)
            dialog = app.screen
            yes_btn = dialog.query_one("#btn_yes", Button)
            yes_btn.press()
            await pilot.pause()
        return app.dialog_result

    result = asyncio.run(run_test())
    assert result is True


def test_confirm_dialog_no_returns_false():
    """No button dismisses with False."""
    from textual.app import App
    from textual.widgets import Button

    class HostApp(App):
        def __init__(self):
            super().__init__()
            self.dialog_result = None

        async def on_mount(self):
            self.push_screen(ConfirmDialog("Proceed?"), callback=self._on_dialog)

        def _on_dialog(self, result: bool):
            self.dialog_result = result

    async def run_test():
        app = HostApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            dialog = app.screen
            no_btn = dialog.query_one("#btn_no", Button)
            no_btn.press()
            await pilot.pause()
        return app.dialog_result

    result = asyncio.run(run_test())
    assert result is False
