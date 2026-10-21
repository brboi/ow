"""PROTOTYPE — throwaway stub of the ow 3 core, just enough to host a TUI plugin.

Implements only what the map has decided and the POC touches:
- pluggy hookspecs from "Plugin interface: events and contributions" (#69);
- `ow ls`, `ow status [-w]`, `ow reset [-w] [--only] [--hard] [--no-fetch] [-n] [-y]`
  with the rules of "ow 3 CLI surface" (#71) and "Git recipe per command" (#72);
- built-in plugins registered by hand from one fixed tuple.

State lives under $OW3_PROTO_HOME (default /tmp/ow3-proto-WIPE-ME): `repos/<name>.git`
and `index.json`. Nothing here is production code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pluggy
import typer

HOME = Path(os.environ.get("OW3_PROTO_HOME", "/tmp/ow3-proto-WIPE-ME"))
REPOS = HOME / "repos"
INDEX = HOME / "index.json"

hookspec = pluggy.HookspecMarker("ow")
hookimpl = pluggy.HookimplMarker("ow")


# ---------------------------------------------------------------- plugin interface (#69)


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path
    branch: Optional[str]  # None = detached HEAD
    base: str


@dataclass(frozen=True)
class Workspace:
    root: Path
    name: str
    repos: tuple[Repo, ...]
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Diagnostic:
    level: str  # info | warning | error
    text: str


class Spec:
    @hookspec
    def ow_settings(self): ...

    @hookspec
    def ow_commands(self, app: typer.Typer): ...

    @hookspec
    def ow_diagnostics(self, ws: Workspace): ...

    @hookspec
    def ow_workspace_created(self, ws: Workspace): ...

    @hookspec
    def ow_refreshed(self, ws: Workspace, written): ...


# ---------------------------------------------------------------- git + workspace model


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def load_index() -> list[Path]:
    return [Path(p) for p in json.loads(INDEX.read_text())] if INDEX.exists() else []


def load_workspace(root: Path) -> Workspace:
    cfg = tomllib.loads((root / ".ow" / "config.toml").read_text())
    bases = cfg.get("bases", {})
    repos = []
    for d in sorted(p for p in root.iterdir() if (p / ".git").is_file()):
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=d, check=False) or None
        base = bases.get(d.name, "origin/HEAD")
        if branch:
            base = git("config", f"branch.{branch}.ow-base", cwd=d, check=False) or base
        repos.append(Repo(d.name, d, branch, base))
    return Workspace(root, root.name, tuple(repos))


def resolve(w: Optional[str]) -> Workspace:
    if w:
        hits = [p for p in load_index() if p.name == w or str(p) == w]
        if len(hits) != 1:
            raise typer.BadParameter(f"{w}: {len(hits)} matching workspaces", param_hint="-w")
        return load_workspace(hits[0])
    cur = Path.cwd()
    for p in (cur, *cur.parents):
        if (p / ".ow" / "config.toml").is_file():
            return load_workspace(p)
    raise typer.BadParameter("not in a workspace; pass -w", param_hint="-w")


def followed_ref(r: Repo) -> str:
    if r.branch:
        up = git("rev-parse", "--abbrev-ref", "@{u}", cwd=r.path, check=False)
        return up or r.base
    return r.base


# ---------------------------------------------------------------- CLI


app = typer.Typer(add_completion=False, no_args_is_help=True)
pm = pluggy.PluginManager("ow")
pm.add_hookspecs(Spec)


@app.command("ls")
def cmd_ls():
    """One line per known workspace: name, path, each repo's branch or detached base."""
    for root in load_index():
        if not (root / ".ow" / "config.toml").is_file():
            print(f"{root.name}\t{root}\t(missing)")
            continue
        ws = load_workspace(root)
        repos = " ".join(f"{r.name}:{r.branch or '(' + r.base + ')'}" for r in ws.repos)
        print(f"{ws.name}\t{root}\t{repos}")


@app.command("status")
def cmd_status(w: Optional[str] = typer.Option(None, "-w", "--workspace")):
    """Per repo: branch, base, upstream, ahead/behind, dirty. Never touches the network."""
    ws = resolve(w)
    print(f"{ws.name}  {ws.root}")
    for r in ws.repos:
        up = git("rev-parse", "--abbrev-ref", "@{u}", cwd=r.path, check=False) if r.branch else ""
        ab = git("rev-list", "--left-right", "--count", f"{r.base}...HEAD", cwd=r.path).split()
        dirty = len(git("status", "--porcelain", cwd=r.path).splitlines())
        print(
            f"  {r.name:<11} {r.branch or '(detached)':<16} base {r.base:<12} "
            f"-{ab[0]} +{ab[1]}  up {up or '-':<16} dirty {dirty}"
        )
    for d in (d for ds in pm.hook.ow_diagnostics(ws=ws) for d in ds):
        if d.level in ("info", "warning"):
            print(f"  {d.level}: {d.text}")


@app.command("reset")
def cmd_reset(
    w: Optional[str] = typer.Option(None, "-w", "--workspace"),
    only: list[str] = typer.Option([], "--only"),
    hard: bool = typer.Option(False, "--hard"),
    no_fetch: bool = typer.Option(False, "--no-fetch"),
    dry_run: bool = typer.Option(False, "-n", "--dry-run"),
    yes: bool = typer.Option(False, "-y", "--yes"),
):
    """Fetch, then put each repo back on the ref it follows."""
    ws = resolve(w)
    repos = [r for r in ws.repos if not only or r.name in only]
    rel = lambda r: r.path.relative_to(ws.root)  # noqa: E731

    if not no_fetch:
        for r in repos:
            ref = followed_ref(r)
            remote, _, branch = ref.partition("/")
            cmd = ["git", "-C", str(rel(r)), "fetch", remote, branch]
            print(" ".join(cmd), flush=True)
            subprocess.run(["git", *cmd[3:]], cwd=r.path, check=True, stdin=subprocess.DEVNULL,
                           capture_output=True)

    plan = []
    for r in repos:
        ref = followed_ref(r)
        head, target = git("rev-parse", "HEAD", cwd=r.path), git("rev-parse", ref, cwd=r.path)
        dirty = git("status", "--porcelain", "--untracked-files=no", cwd=r.path)
        if head == target and not (hard and dirty):
            continue
        dropped = git("rev-list", "--count", f"{ref}..HEAD", "--not", "--remotes", cwd=r.path)
        argv = ["reset", *(["--hard"] if hard else []), ref]
        plan.append((r, argv, dropped, ref, target))

    if not plan:
        print("nothing to do")
        raise typer.Exit(0)
    print()
    for r, argv, dropped, ref, target in plan:
        note = f"   # {ref} = {target[:10]}" + (f", drops {dropped} unpushed commit(s)" if dropped != "0" else "")
        print(f"git -C {rel(r)} {' '.join(argv)}{note}")
    if dry_run:
        raise typer.Exit(0)
    if not yes:
        if not sys.stdin.isatty():
            print("refusing: no terminal to ask on; pass -y", file=sys.stderr)
            raise typer.Exit(2)
        if input("Proceed? [y/N] ").strip().lower() != "y":
            raise typer.Exit(2)
    failed = False
    for r, argv, *_ in plan:
        res = subprocess.run(["git", *argv], cwd=r.path, capture_output=True, text=True)
        print(f"{'ok  ' if res.returncode == 0 else 'FAIL'} git -C {rel(r)} {' '.join(argv)}")
        failed |= res.returncode != 0
    raise typer.Exit(1 if failed else 0)


# ---------------------------------------------------------------- built-in plugins, fixed tuple


def main():
    sys.path.insert(0, str(Path(__file__).parent))
    import tui  # the plugin under test; real ow: one fixed tuple of built-ins

    for plugin in (tui,):
        pm.register(plugin)
    pm.hook.ow_commands(app=app)
    app()


if __name__ == "__main__":
    main()
