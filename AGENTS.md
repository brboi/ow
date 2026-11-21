# ow — Odoo Workspaces · AGENTS.md

ow keeps git repos side by side as worktrees of shared bare repos, grouped into workspaces
(mono-stack, or multi-stack with named stack subdirectories). The core is plain git; Odoo, GitHub
and Runbot knowledge lives in plugins, and the core relays a typed `StackInfo` it never interprets.
Vocabulary: [CONTEXT.md](CONTEXT.md). Commands, flags, exit codes and the spec grammar:
[docs/commands.md](docs/commands.md), the single reference.

Every git command has one shape: session → gather → plan → run_batch → refresh_after.
`cli.py` holds the Typer signatures, `commands/<name>.py` the behaviour. Each module's docstring
and dataclasses are the source for its contract.

## Module map

```
src/ow/
├── cli.py            # every Typer signature, shared options, completers (commands/ never imports it)
├── __main__.py       # main(): OwError → its exit code, KeyboardInterrupt → 130
├── errors.py         # OwError family; each carries its exit code
├── term.py           # message helpers; --json routes progress and plans to stderr
├── core/
│   ├── paths.py      # XDG locations: repos, index, global config, local seed, venvs, cache
│   ├── fs.py         # atomic writes, sidecar locks, path-safety checks
│   ├── cache.py      # file-backed TTL JSON cache
│   ├── json_schema.py# --json envelope (schema v1)
│   ├── git.py        # git runner; check_version() requires ≥ 2.45
│   ├── index.py      # workspace index
│   ├── bases.py      # base branch parsing and inference
│   ├── config.py     # layers: default < global < workspace; a write rewrites the whole file
│   ├── plugins.py    # CoreSpec, plugin records (Setting, GeneratedFile, StackInfo…), Plugins, load_plugins
│   ├── workspace.py  # Workspace, Repo, RepoLocation derived from worktrees; frozen, derive never mutate
│   ├── stacks.py     # multi-stack roots: detection, ports, workspace-level files
│   ├── session.py    # Session: the immutable state every command starts from
│   ├── facts.py      # RepoFacts, gathered once per command, in parallel
│   ├── plan.py       # GitStep/OwStep, RepoPlan, Batch, run_batch prints the whole plan first
│   └── refresh.py    # generated files for a workspace or multi-stack root
├── recipes/          # decision tables: gather_* reads git, plan_* is pure
│   ├── preflight.py  # refusals: operation in progress, dirty state
│   ├── fetch.py      # narrow parallel fetch steps
│   ├── rebase.py     # guarded rebase (also used by retarget)
│   └── removal.py    # pushed-branch rule, branch and workspace removal plans
├── commands/         # one module per command; _-prefixed modules are shared helpers
│   ├── _shared.py _repos.py _targets.py _promote.py _workspace.py
│   └── init add run repo config status refresh files ls cd shell_init rm prune retarget
├── specs/            # hookspecs plugins share, with their merge rules
│   ├── python_env.py # PythonEnvSpec; workspace_venv computes the venv once
│   ├── editors.py    # EditorsSpec
│   └── services.py   # ServicesSpec: DatabaseService, SmtpService
└── plugins/          # built-ins: mise, odoo, postgres, mailsink, github, runbot, vscode, zed, local
    ├── _containers.py# Docker/Podman helper shared by postgres and mailsink
    ├── _github.py    # GitHub and bare-catalog support shared by github, odoo, runbot
    ├── _odoo_repos.py# Odoo repo identity table shared by odoo and runbot
    ├── odoo/
    │   ├── __init__.py   # registered module: every @hookimpl, kept thin
    │   ├── settings.py   # setting declarations, validated reads, diagnostics
    │   ├── release.py    # core detection and release.py AST reading
    │   ├── addons.py     # addons_path discovery, .local/ first, then repos
    │   ├── files.py      # odoorc, odools.toml, pyrightconfig.json, python env, editors
    │   ├── targets.py    # @<base> resolution, base inference, companion core expansion
    │   ├── stacks.py     # ports, promotion pinning, stack name and info
    │   └── runner.py     # the `ow run` command: venv python, else mise exec, else python3
    └── mise.py postgres.py mailsink.py github.py runbot.py vscode.py zed.py local.py

src/tests/            # conftest.py (hermetic autouse fixture), lab.py (real remotes, fake_gh, fake_runbot)
├── scenarios/        # test_<command>.py: CLI integration against real git
├── decisions/        # decision tables
├── plugins/          # settings, generated files, diagnostics, stacks, loader
└── test_shell_init.py# snippets run in real bash and zsh
```

## Plugin loader

`load_plugins` registers `CoreSpec`, `PythonEnvSpec` and `EditorsSpec`, then the fixed tuple from
`_get_builtins()` in `core/plugins.py`. Tuple order is plugin order and matters for `pipe`:
`odoo` precedes `runbot` so companion repos are expanded before Runbot pins them.

- `Plugins.call` runs a hook on every enabled plugin; `errors="collect"` gathers raises as `PluginError`.
- `Plugins.first` returns the first non-None answer and stops there.
- `Plugins.pipe` threads one value through the enabled implementations in order.
- `Plugins.answers(ws)` hands plugins a callable to collect each other's answers.
- Every plugin gets a synthesized `<name>.enabled` setting, default true.

`CoreSpec` hooks:

- `ow_settings`, `ow_diagnostics`, `ow_known_repos`
- `ow_generated_files`, `ow_multi_workspace_files`
- `ow_resolve_target`, `ow_expand_workspace_repos`
- `ow_runner`, `ow_before_run`, `ow_status`, `ow_prune_candidates`
- `ow_stack_values`, `ow_stack_name`, `ow_stack_info`, `ow_is_base_branch`
- events: `ow_workspace_created`, `ow_refreshed`

`PythonEnvSpec.python_environment`, `EditorsSpec.editors_contribution`, and `ServicesSpec.{database_service,smtp_service}` are the shared specs.

## Invariants

- One ownership lock, `.ow/rendered.lock.toml`, per workspace or multi-stack root, taken in `core/refresh.py`. Bytes not matching the lock are the user's and are never rewritten; refresh never deletes a generated file.
- Config precedence is default < global (`$XDG_CONFIG_HOME/ow/config.toml`) < workspace (`.ow/config.toml`).
- `Session.reload()` re-opens a session after git moved branches.

## Tests

```sh
python -m pytest -q src/tests
```

pytest only; `pyproject.toml` sets `pythonpath` and `testpaths`, so no install is needed. The
autouse fixture isolates `HOME`, XDG dirs, git config and `PATH` and blocks live Runbot calls. Tests
need git ≥ 2.45, plus bash and zsh for the shell tests.

## Navigation

- Adding a command: module in `commands/`, signature in `cli.py`, tests in `tests/scenarios/`.
- Adding a plugin: module or package in `plugins/`, implement `CoreSpec` hooks, add it to `_get_builtins()`.
- Changing a decision: edit the recipe's `plan_*`, add a case to `tests/decisions/`.
