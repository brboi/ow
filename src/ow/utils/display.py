from rich.console import Console
from rich.text import Text

console = Console()


def counts(behind: int, ahead: int) -> str:
    b_color = "yellow" if behind > 0 else "dim"
    a_color = "green" if ahead > 0 else "dim"
    return f"[{b_color}]↓{behind}[/] [{a_color}]↑{ahead}[/]"


def print_git_result(alias: str, cmd: str, args: list[str], ok: bool, error: str | None = None) -> None:
    cmd_str = f"  [{alias}] git {cmd} {' '.join(args)}"
    text = Text(cmd_str)
    text.append(" ")
    text.append("✓" if ok else "✗", style="green" if ok else "red")
    console.print(text)
    if not ok and error:
        console.print(f"  Error: {error}")
