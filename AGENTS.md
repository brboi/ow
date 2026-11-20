# ow — Odoo Workspaces · AGENTS.md

ow is a CLI tool that manages Odoo workspaces using git worktrees. This file describes the codebase structure, key abstractions, and how to navigate it for development.

## Architecture

ow follows a plugin-based architecture:

- **Core**: repos, worktrees, workspaces, git recipes, and file refresh.
- **Plugins**: mise, odoo, vscode, zed, local — all contribute data; the core writes.
- **Commands**: one module per command, dispatched by Typer with git operations shown before running.
- **Tests**: hermetic (autouse fixtures), integration (real git), and unit (decision tables).

## Module Map

```
src/ow/
├── __init__.py              # __version__
├── __main__.py              # main(): OwError → exit code
├── cli.py                   # build_app(plugins): Typer, shared options
├── term.py                  # console output, colors, interactive(), confirm()
├── errors.py                # OwError(exit_code), UsageError(2), Refused(1), PluginError(1)
├── core/
│   ├── paths.py             # repos_dir(), index_file(), global_config()
│   ├── git.py               # run/read, GitFailed, check_version(), show()
│   ├── index.py             # read(), add(path), remove(path)
│   ├── bases.py             # parse_base, infer_default_base, followed_ref
│   ├── config.py            # read_layer, resolve_settings, write_*, parse_cli_value
│   ├── plugins.py           # Setting, GeneratedFile, Diagnostic, CoreSpec, Plugins, load_plugins
│   ├── workspace.py         # Repo, Workspace, RepoLocation, find_root, derive_repos, build_workspace
│   ├── session.py           # Session, open_session
│   ├── facts.py             # RepoFacts, gather_facts
│   ├── plan.py              # GitStep, OwStep, RepoPlan, Batch, run_batch, Outcome
│   └── refresh.py           # project(), refresh(): collect, lock, write, events
├── recipes/                 # Pure planners: facts → RepoPlan
│   ├── follow.py            # followed_ref: which ref a repo follows
│   ├── preflight.py         # Refusals: operations in progress, dirty state
│   ├── fetch.py             # Narrow per-remote fetch steps
│   ├── switch.py            # Branch lookup, all-or-nothing pre-flight
│   ├── pull.py              # Fast-forward logic
│   ├── reset.py             # Reset logic with ref following
│   ├── rebase.py            # Upstream step, base step, --base, --autostash
│   └── removal.py           # Pushed rule, rm/prune logic
├── commands/                # CLI: session → gather → recipe → run_batch → refresh
│   ├── _shared.py           # refresh_workspace(), refresh_after(), helpers
│   ├── init.py              # ow init
│   ├── repo.py              # ow repo add/rm/ls
│   ├── config.py            # ow config get/set/unset
│   ├── status.py            # ow status
│   ├── fetch.py             # ow fetch
│   ├── switch.py            # ow switch
│   ├── pull.py              # ow pull
│   ├── rebase.py            # ow rebase
│   ├── reset.py             # ow reset
│   ├── refresh.py           # ow refresh
│   ├── files.py             # ow files
│   ├── ls.py                # ow ls
│   ├── cd.py                # ow cd
│   ├── shell_init.py        # ow shell-init
│   ├── rm.py                # ow rm
│   └── prune.py             # ow prune
├── specs/
│   ├── mise.py              # MiseSpec, MiseEnvironment
│   └── editors.py           # EditorsSpec, EditorContribution, DebugLaunch
└── plugins/                 # Built-ins: mise, odoo, vscode, zed, local
    ├── mise.py
    ├── odoo.py
    ├── vscode.py
    ├── zed.py
    └── local.py

src/tests/
├── conftest.py              # Hermetic git (autouse), lab fixture
├── lab.py                   # Lab: helpers, remotes, odoo-core fixture
├── scenarios/               # test_<command>.py — CLI integration tests
├── decisions/               # test_<recipe>.py — Pure function tests
├── plugins/                 # test_<plugin>.py — Plugin tests
└── test_shell_init.py       # Real bash tests
```

## Key Abstractions

### Records (`core/plugins.py`)

**`Setting`**: A configuration key.

```python
@dataclass(frozen=True)
class Setting:
    key: str                                    # "<plugin>.<name>"
    type: type                                  # str | int | bool | list
    default: object | Callable[[Workspace], object] | None
    help: str
```

**`GeneratedFile`**: A file ow writes.

```python
@dataclass(frozen=True)
class GeneratedFile:
    path: PurePosixPath                         # workspace-relative
    data: bytes
    mode: int = 0o644
```

**`Diagnostic`**: A message.

```python
@dataclass(frozen=True)
class Diagnostic:
    level: Literal["info", "warning", "error"]
    text: str
```

### Plugin Loader (`core/plugins.py`)

**`CoreSpec`**: The hookspec every plugin implements. Plugins contribute `Setting`s, `GeneratedFile`s, `Diagnostic`s, and `KnownRepo`s. Events: `ow_workspace_created`, `ow_refreshed`.

**`Plugins`**: Manages the plugin manager. `settings()` returns all plugin settings (including `<plugin>.enabled`). `call(hook, ws)` runs a hook, skipping disabled plugins.

**`BUILTINS`**: The fixed tuple of built-in plugins: mise, odoo, vscode, zed, local.

### Workspace and Repos (`core/workspace.py`)

**`Repo`**: Describes one repo in a worktree.

```python
@dataclass(frozen=True)
class Repo:
    name: str                                   # e.g. "community"
    path: Path                                  # worktree path
    branch: str | None                          # current branch (None if detached)
    base: str                                   # fully qualified: "origin/18.0"
```

**`Workspace`**: Immutable snapshot of the workspace state.

```python
@dataclass(frozen=True)
class Workspace:
    root: Path
    name: str                                   # directory name
    repos: Mapping[str, Repo]
    settings: Mapping[str, object]              # resolved and typed
```

**`RepoLocation`**: A repo's bare and worktree paths. Derived from `.git` gitdir.

### Session (`core/session.py`)

**`Session`**: The context every command starts with: workspace, plugins, settings, and utilities.

```python
@dataclass(frozen=True)
class Session:
    workspace: Workspace
    plugins: Plugins
    warnings: list[str]
```

### Config (`core/config.py`)

**Precedence**: built-in default < global (`$XDG_CONFIG_HOME/ow/config.toml`) < workspace (`.ow/config.toml`).

**Layers**: `Layer` dataclass with `values: Mapping[str, object]` and a writer.

### Plans and Steps (`core/plan.py`)

**`GitStep`**: One git command to run.

**`OwStep`**: A step with metadata (branch name, base).

**`RepoPlan`**: Per-repo: steps to run, refusals, notes, owned branches.

**`Batch`**: Plan for all repos: `run_batch()` executes in parallel (repos) and serial (per-repo steps).

**`Outcome`**: Result: exit code, written branches, whether repos were mutated.

### Facts (`core/facts.py`)

**`RepoFacts`**: Immutable git reads: branch, base, upstream, ahead/behind, dirty count, in-progress operations.

Computed once per command, never re-read, fed to recipes.

### Refresh (`core/refresh.py`)

**`project(ws, plugins)`**: Collects `GeneratedFile`s from all plugins, merges them, checks ownership against the lock (`.ow/rendered.lock.toml`), and returns files to write.

**`refresh(ws, plugins)`**: Calls `project()`, writes files, updates lock, fires event, returns exit code.

---

## Commands Reference

Every command follows this pattern:

1. **Open session**: `open_session(plugins, workspace_arg)` — validates git version, prints warnings.
2. **Gather facts**: Read git state for each repo.
3. **Recipe**: Build a plan from facts.
4. **Run batch**: Execute plan, collect outcome.
5. **Refresh**: Update generated files if outcome.mutated.

Exit codes:
- **0**: success
- **1**: failure (repo refused or failed)
- **2**: usage error or declined prompt

| Command | Purpose | Mutates | Prompts |
|---|---|---|---|
| `ow init [DIR] [REPO…] [--base] [--branch]` | Create/adopt workspace | yes | no |
| `ow repo add <name> [<url>] [--base]` | Register/clone repo | yes | no |
| `ow repo rm <name> [-n] [-y]` | Remove repo worktree | yes | yes |
| `ow repo ls` | List known repos | no | — |
| `ow config get\|set\|unset <key>` | Read/write settings | yes (set/unset) | no |
| `ow status [-w] [--only]` | Show repo state | no | — |
| `ow fetch [-w] [--only]` | Fetch all repos | no | no |
| `ow switch [BRANCH \| -c BRANCH \| --detach] [-n]` | Switch branch | yes | no |
| `ow pull [-w] [--only] [--no-fetch] [-n]` | Fast-forward | yes | no |
| `ow rebase [-w] [--only] [--base] [--autostash] [-n] [-y]` | Rebase onto base | yes | yes |
| `ow reset [-w] [--only] [--hard] [-n] [-y]` | Reset to ref | yes | yes |
| `ow refresh [-w]` | Rewrite generated files | no | no |
| `ow files [-w] [--diff]` | List file states | no | — |
| `ow ls` | List workspaces | no | — |
| `ow cd [WS]` | Print workspace path | no | — |
| `ow shell-init {SHELL}` | Shell integration | no | — |
| `ow rm <WS> [-n] [-y]` | Remove workspace | yes | yes |
| `ow prune [-n] [-y]` | Machine-wide cleanup | yes | yes |

---

## Tests

### Test Layout

- **`conftest.py`**: Hermetic git fixture (`git_dir`), lab fixture, helpers.
- **`lab.py`**: Helpers for setting up test remotes, clones, and workspaces.
- **`scenarios/test_<command>.py`**: CLI integration tests against real git. Use `CliRunner` (Typer).
- **`decisions/test_<recipe>.py`**: Pure function tests for decision tables (follow, preflight, switch lookup, rebase upstream, pushed rule).
- **`plugins/test_<plugin>.py`**: Plugin tests (settings, generated files, diagnostics).

### Run Tests

```sh
cd /home/boi/dev/brboi/ow
python -m pytest -q src/tests
# Output: 265 passed
```

No linting, formatting, or builds; pytest only.

---

## Navigation Tips

- **Adding a command**: Add module in `commands/`, register in `cli.py`, write tests in `tests/scenarios/`.
- **Adding a plugin**: Create module in `plugins/`, implement hookspecs from `core/plugins.py`, add to `BUILTINS`.
- **Decision tables**: Recipes in `recipes/` implement them; unit tests in `tests/decisions/`.
- **Workspace state**: Read `core/workspace.py` (`Workspace`, `Repo`), never mutate — it's frozen.
- **Config**: `core/config.py` — precedence is built-in < global < workspace.
- **Output files**: `core/refresh.py` — ownership lock is `.ow/rendered.lock.toml`.

---

## Vocabulary

See [CONTEXT.md](CONTEXT.md): Workspace, Repo, Remote, Known repo, Base, Default base, Branch, Upstream, Plan, Plugin, Generated file, Setting, Refresh, Odoo core.
