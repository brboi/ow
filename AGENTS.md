# ow — Odoo Workspaces · AGENTS.md

ow keeps git repos side by side as worktrees of shared bare repos, grouped into workspaces (mono-stack, or multi-stack with named stack subdirectories). The core is plain git. Odoo, GitHub and Runbot knowledge lives in plugins, and the core relays a typed `StackInfo` it never interprets.

This file is the map of the modules and the invariants. The other facts live in one place each:

- Vocabulary: [CONTEXT.md](CONTEXT.md)
- Commands, flags, exit codes, the spec grammar: [docs/commands.md](docs/commands.md)
- Settings and shared services: [docs/configuration.md](docs/configuration.md)
- Files and where ow keeps them: [docs/files.md](docs/files.md)
- Built-in plugins, plugin API, hooks, trust, versions: [docs/plugins.md](docs/plugins.md)
- Requirements and install: [README.md](README.md)

Every command that changes git has one shape: session → gather → plan → run_batch → refresh_after. That includes `init` and `add`: they gather first, print the whole plan, then run it. `cli.py` holds the Typer signatures and `commands/<name>.py` the behaviour. Each module's docstring and dataclasses are the source for its contract.

## Module map

```
src/ow/
├── cli.py            # the Typer signatures, shared options, completers (commands/ never imports it)
├── __main__.py       # main(): OwError → its exit code, KeyboardInterrupt → 130
├── errors.py         # OwError family; each carries its exit code
├── term.py           # message helpers: output, warnings, confirmation, interactivity
├── core/
│   ├── paths.py      # XDG locations
│   ├── fs.py         # atomic writes, sidecar locks, path-safety checks
│   ├── cache.py      # file-backed TTL JSON cache
│   ├── git.py        # git runner; check_version() requires ≥ 2.45; prompts only on a terminal
│   ├── index.py      # workspace index
│   ├── bases.py      # base and spec-range parsing (`base..branch`), base inference
│   ├── config.py     # layers: default < global < workspace < stack, with each value's origin; keeps unknown keys
│   ├── trust.py      # in-repo plugin discovery and location-based trust registry
│   ├── plugins.py    # CoreSpec, plugin records, Plugins (call/first/pipe/answers), load_plugins, in-repo plugin loader
│   ├── container_engine.py # docker or podman detection (`OW_CONTAINER_ENGINE`)
│   ├── workspace.py  # Workspace, Repo, RepoLocation derived from worktrees; immutable, derive never mutate
│   ├── stacks.py     # multi-stack roots: detection, ports, workspace-level files
│   ├── session.py    # Session: the immutable state every command starts from
│   ├── facts.py      # RepoFacts, gathered once per command, in parallel
│   ├── plan.py       # GitStep/OwStep, RepoPlan, Batch; run_batch prints the whole plan first
│   ├── runtime.py    # detached process tracking (.ow/run/*.json, .ow/logs/*.log), stop, log follow
│   └── refresh.py    # generated files and unified lock for a workspace or multi-stack root
├── plugin_api.py     # the public plugin API; plugins import only from here
├── recipes/          # decision tables: gather_* reads git, plan_* is pure
│   ├── preflight.py  # refusals: operation in progress, dirty state, no base
│   ├── fetch.py      # narrow parallel fetch steps
│   ├── rebase.py     # guarded rebase: colleague commits, then the base, or a series change
│   └── removal.py    # pushed-branch rule, branch and workspace removal plans
├── commands/         # one module per command; _-prefixed modules are shared helpers
│   ├── _shared.py _repos.py _specs.py _promote.py
│   ├── _workspace.py # init/add engine: gather, plan, run, undo; worktree states and the one safe cleanup
│   ├── _runtime.py   # Target (an opened workspace + its plugin set), foreground child runner, running-refusals, root removal
│   └── init add run stop logs exec sandbox repo config status refresh files trust ls cd shell_init rm prune rebase
├── specs/            # hookspecs plugins share, with their merge rules
│   ├── python_env.py # PythonEnvSpec (environment, bootstrap, wrap), python.* settings, workspace_venv computes the venv once
│   ├── editors.py    # EditorsSpec
│   └── services.py   # ServicesSpec: DatabaseService, SmtpService
└── plugins/          # built-ins; __init__.py: builtin_plugins() is the ordered list
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
    │   └── runner.py     # the `ow run` runner: venv python, else the environment plugin's wrapper, else python3; --reset
    └── mise.py postgres.py mailsink.py github.py runbot.py vscode.py zed.py local.py sandbox.py

src/tests/            # conftest.py (hermetic autouse fixture), lab.py (real remotes, fake_gh, fake_runbot)
├── scenarios/        # test_<command>.py: CLI integration against real git
├── decisions/        # decision tables, spec grammar
├── plugins/          # settings, generated files, diagnostics, stacks, loader, in-repo plugins, built-ins import only plugin_api
├── test_completion.py# shell completion end to end
├── test_hermetic.py  # host OW_* variables never reach a test
└── test_shell_init.py# snippets run in real bash and zsh
```

## Plugin loader

`load_plugins` registers the hookspecs `CoreSpec`, `PythonEnvSpec`, `EditorsSpec` and `ServicesSpec`, then the tuple from `ow.plugins.builtin_plugins()`. Tuple order is plugin order and matters for `pipe` and `first`. `Plugins.with_repo_plugins` appends the trusted in-repo plugins of a workspace after the built-ins. Commands go through the one plugin set their `Session` carries (`session.plugins`). The hooks, their call style and error policy are in `CoreSpec`'s docstrings and tabulated in [docs/plugins.md](docs/plugins.md#hook-reference).

- `Plugins.call` runs a hook on every enabled plugin. `errors="collect"` gathers raises as `PluginError`.
- `Plugins.first` returns the first non-None answer and stops there.
- `Plugins.pipe` threads one value through the enabled implementations in order.
- `Plugins.answers(ws)` hands plugins a callable to collect each other's answers.
- Every plugin gets a synthesized `<name>.enabled` setting. Every other setting key is `<name>.`-prefixed. The `python.` prefix and the plugin name `python` are reserved for ow.

## Invariants

- One `.ow/` directory per workspace root. Stacks of a multi-stack workspace have none: their repos and settings live under `[stacks.<name>]`, and their locked paths are prefixed `<name>/` in the root lock.
- Bytes not matching the lock are the user's and are never rewritten. A refresh never deletes a generated file.
- A value set in any config layer (`Workspace.origins`, `is_explicit`) is never overwritten by a service's value. A service only fills what is still at its default.
- A config rewrite keeps every key it does not know. Only `ow config unset` removes one.
- ow never removes a non-empty worktree directory it cannot prove holds no work. `cleanup_worktree` is the only removal path for creation leftovers.
- In-repo plugins run only from a trusted location. A corrupt trust file is an error and is never rewritten.
- Built-in plugins import API names from `ow.plugin_api`, like an external plugin does. `tests/plugins/test_builtin_imports.py` enforces it.
- `Session.reload()` re-opens a session after git moved branches.

## Tests

```sh
python -m pytest -q src/tests
```

pytest only. `pyproject.toml` sets `pythonpath` and `testpaths`, so no install is needed. The autouse fixture isolates `HOME`, the XDG directories, git config, `PATH` and the host's `OW_*` variables, and blocks live Runbot calls. CI runs `ruff` and the tests, with git at its 2.45.4 floor on Ubuntu and Alpine. Bash and zsh are needed for the shell tests.

## Where to change what

- Adding a command: a module in `commands/`, its signature in `cli.py`, tests in `tests/scenarios/`, its section in `docs/commands.md`.
- Adding a built-in plugin: a module or package in `plugins/`, its entry in `builtin_plugins()`, a row in the table of `docs/plugins.md`.
- Adding or changing a hook: `CoreSpec` (its docstring states the error policy), then the table in `docs/plugins.md`.
- Changing a decision: the recipe's `plan_*`, plus a case in `tests/decisions/`.
