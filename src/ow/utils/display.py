from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
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


@contextmanager
def task_progress(label: str, total: int, *, console: Console | None = None) -> Iterator:
    """A `label n/total` counter, silent when the output is not a terminal.

    Replaces the fixed-text spinner: the count only became possible once
    parallel_per_repo started reporting completions as they happen (#25).
    """
    target = console if console is not None else globals()["console"]
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("{task.completed}/{task.total}"),
        console=target,
        disable=not target.is_terminal,
    )
    with progress:
        task_id = progress.add_task(label, total=total)
        yield lambda: progress.advance(task_id)
