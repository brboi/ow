from rich.console import Console
from rich.text import Text


def _make_console(**kwargs) -> Console:
    """Build a Console safe for CLI output.

    highlight=False keeps Rich's ReprHighlighter from repainting branch names
    and numbers ("origin/18.0"); soft_wrap=True keeps long status lines and
    URLs on one line when stdout is redirected (Rich falls back to 80 columns).
    """
    return Console(highlight=False, soft_wrap=True, **kwargs)


console = _make_console()
err_console = _make_console(stderr=True)


def counts(behind: int, ahead: int) -> str:
    b_color = "yellow" if behind > 0 else "dim"
    a_color = "green" if ahead > 0 else "dim"
    return f"[{b_color}]↓{behind}[/] [{a_color}]↑{ahead}[/]"


def print_git_result(alias: str, cmd: str, args: list[str], ok: bool, error: str | None = None) -> None:
    cmd_str = f"  [{alias}] git {cmd} {' '.join(args)}"
    text = Text(cmd_str)
    text.append(" ")
    text.append("✓" if ok else "✗", style="green" if ok else "red")
    if ok:
        console.print(text)
        return
    err_console.print(text)
    if error:
        # git stderr is data, not markup: Text() keeps "[rejected]" intact.
        err_console.print(Text(f"  Error: {error}"))
