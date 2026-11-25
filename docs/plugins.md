# Plugins

An ow plugin is a Python module whose `@hookimpl` functions ow calls at defined moments: to declare settings, generate files, resolve specs, run a workspace, report status and more. The built-in plugins use the same hooks. A repo can also ship a plugin. ow finds it in a workspace's checkout, asks you to trust it once and loads it next to the built-ins.

This page lists the built-in plugins, documents the plugin API (v1) and states the version policy. Plugins import only from `ow.plugin_api`.

- [Built-in plugins](#built-in-plugins)
- [A complete example](#a-complete-example)
- [Discovery](#discovery)
- [Trust](#trust)
- [Versioning and compatibility](#versioning-and-compatibility)
- [Settings](#settings)
- [Hook reference](#hook-reference)
- [Data records](#data-records)
- [Testing](#testing)
- [Non-goals](#non-goals)

## Built-in plugins

Plugins run in this order. The order matters for `pipe` and `first` hooks: `odoo` comes before `runbot`, so companion repos are expanded before Runbot pins them. In-repo plugins come last.

| Plugin | What it does |
|---|---|
| `mise` | Builds the shared virtualenv and wraps commands with `mise exec`. Writes `mise/conf.d/00-ow.toml`. Needs mise ≥ 2026.8.13 |
| `odoo` | Detects the Odoo core. Writes `odoorc`, `odools.toml`, `pyrightconfig.json` and `requirements-dev.txt`. Expands `@<base>` specs and companion repos, allocates ports, provides the `ow run` runner and `--reset` |
| `postgres` | Starts the shared PostgreSQL and pgweb containers and answers `database_service` |
| `mailsink` | Starts the shared Mailpit container and answers `smtp_service` |
| `github` | Resolves PR specs and sister PRs, reports PR status, proposes merged PRs to `ow prune`. Needs `gh` |
| `runbot` | Resolves Runbot URLs, pins companion repos to the commit Runbot built against, reports build status |
| `vscode` | Writes `.vscode/settings.json`, `.vscode/launch.json` and `<workspace>.code-workspace` |
| `zed` | Writes `.zed/settings.json` and `.zed/debug.json` |
| `local` | Copies `~/.config/ow/local` into a new workspace's `.local/` |
| `sandbox` | Declares the `sandbox.*` settings for [`ow sandbox`](commands.md#ow-sandbox) |

The files each plugin writes are in [Generated files](files.md). The settings are in [Configuration](configuration.md#settings).

## A complete example

A repo with this `ow_plugin.py` at its root adds a setting, a generated file, a status line and a runner:

```python
"""hello: a minimal in-repo ow plugin with one setting, one file, one status line and a runner."""

from ow.plugin_api import GeneratedFile, RunCommand, Setting, StatusInfo, hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "hello"


@hookimpl
def ow_settings():
    return [Setting(key="hello.greeting", type=str, default="hello", help="Word written to HELLO.txt")]


@hookimpl
def ow_generated_files(ws):
    text = f"{ws.settings['hello.greeting']} from {ws.name}\n"
    return [GeneratedFile(path="HELLO.txt", data=text.encode())]


@hookimpl
def ow_status(ws):
    if ws is not None:
        return [StatusInfo(plugin="hello", scope="workspace", subject=ws.name, summary=f"{len(ws.repos)} repo(s)")]


@hookimpl
def ow_runner(ws, args):
    return RunCommand(argv=("echo", ws.settings["hello.greeting"], *args), cwd=ws.root)
```

Put it in a workspace and use it:

```sh
ow init hello                 # warning: untrusted plugin …/hello/ow_plugin.py; run `ow trust` to enable it
ow trust -y                   # trusts the plugin file, then refreshes: "wrote HELLO.txt"
ow status                     # … "# hello: 1 repo(s)" and "# plugin: hello (…/hello/ow_plugin.py)"
ow config get hello.greeting  # hello
ow run -- world               # "# starting <ws> (plugin hello: …)" then "hello world"
```

Rules visible in the example:

- A hook implementation declares **any subset** of the hook's arguments, by name: `ow_generated_files(ws)`, not the full `(ws, answers, multi)`.
- A parameter with a default value is passed too when the hook has an argument of that name (`multi=None`). Otherwise it keeps its default.
- Returning `None` means "nothing to say".
- Hooks are plain module-level functions. The module itself is the plugin.

## Discovery

ow looks for a plugin in the worktree of **each repo of a workspace**, including each repo of each stack:

1. `[tool.ow] plugin = "path/to/plugin.py"` in the repo's `pyproject.toml`, relative to the repo root; else
2. `ow_plugin.py` at the repo root.

One plugin per repo. A path that is absolute, escapes the repo (`..`, symlinks) or does not exist is ignored. The module defines:

| Name | Required | Meaning |
|---|---|---|
| `OW_API_VERSION` | yes | The integer API version the plugin was written for: `1`. Write it as a literal |
| `PLUGIN_NAME` | no | The plugin's name. It prefixes the plugin's settings and its `[<name>]` config table. It matches `[A-Za-z0-9_-]+` and defaults to the repo's name. `python` is reserved |

A plugin loads after the built-ins, so it comes last in plugin order. In a multi-stack workspace, a trusted plugin from one stack loads for every stack. `<name>.enabled = false` switches it off like any plugin.

ow loads the plugin as a single file, by path. Its directory is **not** on `sys.path`, so a sibling module cannot be imported: keep the plugin in one file. ow writes no `__pycache__` into your repo.

When the import fails (a syntax error, an exception, a missing import), ow prints `warning: skipping plugin <path>: failed to import (<error>)` and carries on without it. An invalid or already registered name does the same. A plugin whose `ow_settings` raises, or returns anything other than a list of `Setting`, is skipped alone with a warning. `ow status` and `ow run` name the loaded in-repo plugins (`# plugin: hello (<path>)`).

## Trust

A plugin is arbitrary Python code that runs with your user's rights every time ow opens the workspace. ow therefore runs an in-repo plugin only from a **trusted location**. Trust works like an editor's workspace trust: it is granted to a *location*, not to a file's content. The `ow trust` command is in [Commands](commands.md#ow-trust).

- An untrusted plugin is skipped with `warning: untrusted plugin <path>; run `ow trust` to enable it`. A command never prompts in the middle of its run.
- A trusted directory covers every plugin beneath it.
- The registry is `$XDG_STATE_HOME/ow/trusted-plugins.toml` (mode `0600`). It records canonical paths. A corrupt registry is an error: ow reports it and rewrites nothing.

> **Trusting a directory trusts everything that ever shows up under it.** ow does not look at what is in the file. A trusted plugin stays trusted after you edit it, after you switch branch and after `ow add enterprise#12345` checks out a pull request that adds or changes an `ow_plugin.py`. That code runs on the next ow command, with no prompt. Trust a plugin file for a repo you control. Trust a directory only when everything that can land under it is code you would run anyway.

## Versioning and compatibility

- A plugin declares `OW_API_VERSION = 1`. ow loads a plugin whose version is an integer inside `ow.plugin_api.SUPPORTED_API_RANGE` (currently `(1, 1)`). Any other value, or none, skips the plugin with `warning: skipping plugin <path>: incompatible OW_API_VERSION=<value> (ow supports v1)`. The command still succeeds. `ow.plugin_api.OW_API_CURRENT` is the version ow itself implements.
- **API v1 is frozen from 3.0.0rc1. It may still change during the 3.0 betas.**
- After the freeze, 3.x only *adds*: new hooks, new optional hook arguments, new optional fields with defaults, new exports. Nothing a v1 plugin relies on is renamed or removed in 3.x.
- Anything slated for removal first emits a deprecation warning in a 3.x release. It is removed in 4.0, which raises `OW_API_VERSION`.
- Everything in this page's [hook reference](#hook-reference) and [data records](#data-records) is public API. Anything not exported from `ow.plugin_api` (`ow.core.*`, `ow.specs.*`, `ow.plugins.*`, `ow.commands.*`) is internal and may change in any release.
- `ow sandbox` is [experimental](commands.md#ow-sandbox) and outside this policy. So is the `ow_sandbox_mounts` hook that feeds it.

## Settings

A plugin declares settings from `ow_settings` and reads them from `ws.settings`:

```python
Setting(key="hello.greeting", type=str, default="hello", help="…", unique=False)
```

- **Prefix.** Every key starts with `<plugin name>.`. A setting outside the prefix, or whose key is already declared, is dropped with `warning: plugin hello: ignoring setting 'odoo.greeting' (keys must start with 'hello.')`. The plugin still loads. A plugin can never shadow another plugin's setting. The `python.` prefix belongs to ow.
- **`enabled`.** ow synthesizes `<plugin name>.enabled` (bool, default `true`) for every plugin. Do not declare it. When it is `false` for a workspace, none of the plugin's hooks run there.
- **Types.** `str`, `int`, `bool` or `list`. A config value of the wrong type keeps the default and warns.
- **Default.** A value, or a function of the `Workspace` (`odoo.db_name` defaults to the workspace name). The function's result is `None` where there is no workspace.
- **`unique=True`**, meaningful for `str` only. The value must differ across workspaces. When ow creates a workspace or stack whose value is still the default and equals what another indexed workspace resolves to, it writes `<value>-2` (next free suffix) into the new config. A value the user set is never changed. A workspace that lacks your in-repo plugin does not count.
- **`renamed_to="<new key>"`** marks a setting that no longer exists. A config that still sets it is refused with `<key> was renamed to <new key>`. The setting has no value, is never listed and cannot be set. The `mise` plugin uses it for `mise.python` and `mise.shared_venv`.
- **Explicit or default.** `ws.is_explicit("hello.greeting")` is true when any config layer set the key. `ws.origins` maps each explicit key to its layer (`global`, `workspace`, `stack`). A plugin that takes values from a service fills only the settings the user left at their default. An explicit value, even one equal to the default, always wins.

Users set them with `ow config set hello.greeting hi`. See [Configuration](configuration.md). A key of a plugin that is not loaded stays in the config file: [Unknown keys](configuration.md#unknown-keys-and-wrong-types).

## Hook reference

Plugins run in the order of the [built-in table](#built-in-plugins), then the in-repo plugins. A plugin is skipped for a workspace where `<name>.enabled` is false.

**Call style**

- **call**: every enabled plugin is called, in plugin order. `None` answers are dropped and the others are gathered.
- **first**: plugins are called in order until one answers non-`None`. That answer wins and later plugins are not called.
- **pipe**: one value is threaded through every plugin in order. Each non-`None` return replaces it for the next.
- **answers**: the core does not call the hook directly. A consumer asks `answers("hook_name")` and gets every enabled plugin's answer for one workspace.

**Error policy**

- **Propagates**: an exception aborts the command as `plugin <name> failed in <hook>: …` (exit 1). A `UsageError` raised by the hook keeps its own exit code (2).
- **Collected**: an exception becomes a warning naming the plugin. The command continues with the other plugins' answers.

**Arguments.** `ws` is the `Workspace` (a workspace or one stack). `multi` is a `MultiWorkspace` when the command runs at a multi-stack root. `answers` is injected when you declare it and a workspace is known. `answers("python_environment")` returns every enabled plugin's answer for that workspace. `answers.for_workspace(other_ws)` binds it to another workspace of the same session, such as a sibling stack. `answers.implementers("hook")` lists the enabled plugins that implement a hook.

`ow_multi_workspace_files` is the one hook that gets `answers` without a `ws`. Its `answers` is bound to the first stack of `multi`. It is not called for a root without stacks.

### Core hooks

| Hook → returns | Called when | Style | Errors |
|---|---|---|---|
| `ow_settings()` → `list[Setting]` | ow collects the plugins' settings (loading, `ow config`, every command) | call | Propagates for a built-in. Collected for an in-repo plugin: it alone is skipped with a warning |
| `ow_generated_files(ws, answers, multi)` → `list[GeneratedFile \| Diagnostic]` | Every refresh of a workspace or stack | call | Propagates |
| `ow_diagnostics(ws, multi)` → `list[Diagnostic]` | Opening a session and every refresh | call | Collected |
| `ow_known_repos()` → `list[KnownRepo]` | Resolving a repo name or spec, `ow repo add`, completion | call | Propagates |
| `ow_workspace_created(ws)` → `None` | After `ow init`, `ow add` or `ow run --tmp` creates a workspace or stack | call | Propagates |
| `ow_refreshed(ws, owned)` → `None` | After a refresh. `owned` is every path ow wrote or found up to date | call | Propagates |
| `ow_resolve_spec(spec, known_repos, settings)` → `ResolvedSpec \| None` | Resolving each item of a spec (`enterprise#123`, `@18.0`, a URL) | first | Propagates |
| `ow_expand_workspace_repos(repos, known_repos, settings)` → `tuple[ResolvedRepo, ...] \| None` | After resolving, to add companion repos (the Odoo core next to an addon PR) | pipe | Propagates |
| `ow_runner(ws, multi, args, answers)` → `RunCommand \| None` | `ow run`. `args` are the extra arguments | first | Propagates |
| `ow_running(ws)` → `list[str] \| None` | Asking whether a plugin-managed process runs: `ow run` (refuse a double start, follow a self-detaching run), `ow stop`, `ow logs -f`, `ow rm`, `ow prune` | call | Collected. A warning names the plugin and it counts as not running |
| `ow_stop(ws, multi)` → `RunCommand \| list[str] \| None` | `ow stop`, and the end of a foreground self-detaching `ow run`. A `RunCommand` is executed, a list of lines is printed | first | Propagates |
| `ow_stop_services(ws)` → `list[str] \| None` | `ow stop --services` and `--all`. `ws` is `None` | call | Collected |
| `ow_logs(ws, multi)` → `list[tuple[str, Path]] \| None` | `ow logs`, and the follow of a foreground self-detaching `ow run`. `(label, log_path)` pairs | first | Propagates |
| `ow_before_run(ws, workspaces)` → `list[str] \| None` | Before `ow run` and `ow sandbox` start. Prepares shared services and returns info lines | call | Collected. It warns and the run goes on |
| `ow_reset_preview(ws, answers)` → `list[str] \| None` | `ow run --reset`, before the confirmation: one line per thing `ow_reset_run` removes | call | Propagates. A failing preview aborts `--reset` |
| `ow_reset_run(ws, answers)` → `list[str] \| None` | `ow run --reset`, once confirmed. Removes local runtime state and returns info lines. Without `ow_reset_preview`, the confirmation lists `<plugin>: local state` | call | Propagates |
| `ow_status(ws, facts, spec, offline, known_repos, settings)` → `list[StatusInfo]` | `ow status`. For `ow status SPEC`, `ws` is `None` and `spec` is set | call | Collected. It contributes nothing |
| `ow_prune_candidates(workspaces, offline)` → `list[PruneCandidate]` | `ow prune` | call | Collected |
| `ow_stack_values(ws, ws_values, siblings, pending, global_settings, repo_paths, repo_names)` → `dict[str, object] \| None` | Creating or promoting a stack: settings to pin explicitly (coordinated ports, database name) | call | Propagates |
| `ow_stack_name(ws)` → `str \| None` | Promotion to multi-stack: suggest the stack directory name | first | Propagates |
| `ow_stack_info(ws)` → `StackInfo \| None` | Showing a stack (`ow ls`, `ow status`, `ow run`): ports and a note | call, merged | Propagates |
| `ow_is_base_branch(repo, branch)` → `bool \| None` | Deciding whether a branch is an upstream series, checked out detached | first | Propagates |
| `ow_sandbox_mounts(ws, answers)` → `SandboxMounts \| None` | `ow sandbox`: extra mounts and variables. Experimental | call, merged | Propagates |
| `ow_multi_workspace_files(multi, answers)` → `list[GeneratedFile \| Diagnostic]` | Every refresh of a multi-stack root: files written at the root | call | Propagates |

Details worth knowing:

- **`ow_generated_files`** returns `GeneratedFile`s (path relative to the workspace root, bytes, mode) and optionally `Diagnostic`s. Ownership, the lock and "never overwrite a hand edit" are ow's job ([Generated files](files.md)).
  - Two plugins producing one path, an absolute path, a path escaping the workspace, or `.ow/config.toml` and the lock are errors.
  - A `Diagnostic` of level `error` blocks the whole refresh: nothing is written and the exit code is 1. A `warning` is printed. An `info` shows in `ow status` as `# <plugin>: <text>`.
- **`ow_status`** returns `StatusInfo(plugin=…, scope=…, subject=…, summary=…)`. `scope` is `workspace`, `repo`, `external` or `service`. Network lookups must honour `offline`.
- **`ow_stack_values`** answers are merged in plugin order. A key already present is never overridden, whether it is the user's or an earlier plugin's. **`ow_stack_info`** answers are merged: `ports` are combined and `note`s are joined with `, `.
- **`ow_resolve_spec`** returns `ResolvedSpec(name, repos, source)` for a spec it recognises and `None` for the rest. `ow_expand_workspace_repos` receives and returns the tuple of `ResolvedRepo`s.
- **A `RunCommand` runs in the foreground with ow's terminal.** Ctrl+C reaches it too. The first Ctrl+C only makes ow keep waiting, so the command can tear down. A second one terminates it (SIGTERM, then SIGKILL after a few seconds). An interrupted run exits 130. `RunCommand.env` is laid over ow's own environment and nothing else. The `env` of `python_environment` answers is **not** applied: put what the command needs in `RunCommand.env`.
- **Files a plugin generates inside a repo's worktree** must be gitignored, or git reports them as untracked. If a tool of the repo rewrites one, it no longer matches ow's bytes and becomes `yours` ([Generated files](files.md)). ow then leaves it alone until it is deleted.

### Run, stop and logs: one hook per subject

`ow_runner`, `ow_stop` and `ow_logs` take `(ws, multi)`:

| Where | Call |
|---|---|
| Mono-stack workspace | `ws=<workspace>, multi=None` |
| Multi-stack root | First `ws=None, multi=<root>`. An answer there covers the whole workspace. With none, once per stack: `ws=<stack>, multi=<root>` |
| A stack addressed alone | `ws=<stack>, multi=<root>` |

At the root, `answers` is bound to the first stack.

- **`self_detaching=True`** on a `RunCommand` means: this command starts background services, then exits.
  - With `ow run -d`, ow runs it and returns.
  - In the foreground, ow runs it, then follows the files `ow_logs` returns, as `ow logs -f` does (the last 50 lines first). It stops following when `ow_running` reports nothing, or on Ctrl+C. Then it calls `ow_stop`. Without `ow_running`, only Ctrl+C ends the follow.
  - If the start fails, ow calls `ow_stop` and exits with the start's exit code. Ctrl+C exits 130.
- Any other `RunCommand` runs on the terminal, or detached under `-d`.
- **`ow_logs`** returns log files only. ow shows or follows them for `-f` and `-n`, which a command could not honour. An empty list means "nothing from this plugin", like `None`: `ow logs` then shows `.ow/logs`.
- **`ow_stop`** returns a `RunCommand` that ow executes, or lines that ow prints. A `RunCommand`'s exit code becomes ow's exit code.

### Shared specs

Six more hooks let plugins cooperate without knowing each other. All are **answers**-style: consumers call `answers("<name>")`. An exception propagates.

| Hook → returns | Consumed by | Merge |
|---|---|---|
| `python_environment(ws)` → `PythonEnvironment \| None` | `ow run`, `ow exec`, the venv resolution, `resolve_stack_python` | `path`, `requirements`, `postinstall` concatenated. `env`, `env_paths`, `aliases`: the later answer wins per key. `python` and `venv_key`: the later non-`None` wins |
| `python_bootstrap(ws, venv, log_to_stderr)` → `bool \| None` | The venv preparation of `ow run` and `ow exec` | The plugin that builds the interpreter and the virtualenv at `venv.path` returns `True`. Others return `None`. ow checks afterwards that the venv exists |
| `python_wrap(ws, argv)` → `tuple[str, ...] \| None` | `ow exec` and the runner, where ow finds no virtualenv | The first answer is used. It returns `argv` prefixed to run inside the environment the plugin manages |
| `editors_contribution(ws, answers)` → `EditorContribution \| None` | `vscode`, `zed` | `launches` concatenated. `excludes` concatenated and deduplicated. `language_servers` merged per server and key, later wins. `python`: the later non-`None` wins |
| `database_service(ws)` → `DatabaseService \| None` | `odoo`, which fills `odoo.db_*` left at their default | The consumer decides. `odoo` takes the first |
| `smtp_service(ws)` → `SmtpService \| None` | `odoo`, which fills `odoo.smtp_*` left at their default | The consumer decides. `odoo` takes the first |

`PythonEnvironment` paths are relative to the workspace root. ow computes the virtualenv once, from the merged answers, and the generated files, the runner and `ow exec` all use that result. The version comes from `python.version`, bounded by `PythonEnvironment.python`. The venv is shared under `$XDG_DATA_HOME/ow/venvs` only when `python.shared_venv` is true **and** an enabled plugin implements `python_bootstrap` (today `mise`). Otherwise it is `<workspace>/.venv`.

`resolve_stack_python(ws, answers)` gives a stack's interpreter path. `answers` is the one ow hands to your hook. `ws` may be a sibling stack. `workspace_venv(ws, env, *, shareable=True)` resolves a `WorkspaceVenv` from a merged `PythonEnvironment`.

## Data records

Exported by `ow.plugin_api`. All are immutable dataclasses with **keyword-only** fields. Build them as `Setting(key=…, type=…)`, never positionally, so ow can add fields in any 3.x release. Fields appear in declaration order. `= x` is a default:

| Record | Fields |
|---|---|
| `Setting` | `key`, `type`, `default`, `help`, `unique = False`, `renamed_to = None` |
| `GeneratedFile` | `path` (a `str` or `PurePosixPath`, relative), `data: bytes`, `mode = 0o644` |
| `Diagnostic` | `level` (`"info"`, `"warning"` or `"error"`), `text` |
| `KnownRepo` | `name`, `url` |
| `ResolvedRepo` | `name`, `url = None`, `branch = None`, `base = None`, `remote = None`, `fetch_ref = None`, `commit = None`, `explicit = True`, `pr = None`, `head_remote = None`, `head_url = None` |
| `ResolvedSpec` | `name`, `repos: tuple[ResolvedRepo, ...]`, `source` |
| `StackInfo` | `ports = {}`, `note = None` |
| `RunCommand` | `argv: tuple[str, ...]`, `cwd: Path`, `env = {}`, `self_detaching = False` |
| `StatusInfo` | `plugin`, `scope`, `subject`, `summary` |
| `PruneCandidate` | `workspace: Path`, `reason` |
| `SandboxMounts` | `ro_paths = ()`, `rw_paths = ()`, `env = {}` |
| `PythonEnvironment` | `env = {}`, `env_paths = {}`, `path = ()`, `python = None` (supported `((min major, minor), (max major, minor))`), `venv_key = None`, `requirements = ()`, `postinstall = ()`, `aliases = {}` |
| `EditorContribution` | `launches = ()`, `language_servers = {}`, `excludes = ()`, `python = None` |
| `DebugLaunch` | `name`, `program: Path`, `args`, `cwd: Path`, `env` |
| `DatabaseService` | `host`, `port`, `user`, `password`, `web_url = None`, `drop = None` (callable dropping a database), `admin_user = ""`, `admin_password = ""` |
| `SmtpService` | `host`, `port`, `web_url = None` |
| `WorkspaceVenv` | `python_version`, `path`, `diagnostics = ()`. `is_local(ws)` |

`ResolvedRepo.head_remote` names the remote that holds a PR's head branch (`origin`, or the fork owner's login). `head_url` is the clone URL ow adds to the bare when that remote is missing.

What plugins receive. These are read-only keyword-only dataclasses:

| Record | Fields |
|---|---|
| `Workspace` | `root: Path`, `name`, `repos: tuple[Repo, ...]`, `settings` (mapping), `is_stack`, `origins`. `is_explicit(key)` |
| `Repo` | `name`, `path` (the worktree), `branch` (`None` = detached), `base` |
| `MultiWorkspace` | `root`, `name`, `stacks: tuple[Workspace, ...]` |
| `RepoFacts` | `name`, `path`, `bare`, `remotes`, `head`, `branch`, `upstream`, `upstream_ab`, `base`, `default_base`, `explicit_base`, `operation`, `modified`, `untracked`, `problem`, `unpushed_upstream = None` |
| `Answers` | Callable `answers("hook")` → `list`. `for_workspace(ws)`, `implementers("hook")` |

Errors: `UsageError` (exit 2: a mistake the user can fix) and `PluginError` (exit 1). Raise `UsageError("message")` for a clean one-line error. Any other exception is reported as a `PluginError` naming your plugin and the hook.

Helpers and constants: `hookimpl`, `OW_API_CURRENT`, `SUPPORTED_API_RANGE`, `resolve_stack_python(ws, answers)`, `workspace_venv(ws, env, *, shareable=True)`, `detect_container_engine()` (`docker`, `podman` or `None`) and `is_port_free(port, host="127.0.0.1")`. The package ships `py.typed`, so type checkers see these annotations.

## Testing

**Unit.** A hook is a plain function. Import the plugin file and call it with a hand-built `Workspace`. Only `ow` itself needs to be importable.

```python
import sys
from pathlib import Path

sys.path.insert(0, "path/to/repo")           # the directory holding ow_plugin.py
import ow_plugin
from ow.plugin_api import Workspace


def test_generated_file(tmp_path: Path) -> None:
    ws = Workspace(root=tmp_path, name="demo", repos=(), settings={"hello.greeting": "hi"})
    (file,) = ow_plugin.ow_generated_files(ws)
    assert file.path.as_posix() == "HELLO.txt"
    assert file.data == b"hi from demo\n"
```

**End to end, without touching your machine.** Give ow a throwaway home and a local remote:

```sh
export HOME=/tmp/play/home XDG_CONFIG_HOME=/tmp/play/home/.config XDG_DATA_HOME=/tmp/play/home/.local/share \
       XDG_STATE_HOME=/tmp/play/home/.local/state XDG_CACHE_HOME=/tmp/play/home/.cache
export GIT_CONFIG_GLOBAL=/tmp/play/gitconfig           # [protocol "file"] allow = always, user.name/email
export OW_CONTAINER_ENGINE=none                          # a name that is not installed: `ow run` finds no container engine
mkdir -p /tmp/play/ws && cd /tmp/play/ws
ow repo add hello file:///path/to/hello-repo
ow init hello && ow trust -y && ow status && ow config
```

The trust registry lives under `XDG_STATE_HOME`, so trusting in the throwaway home trusts nothing in your real one. ow's own tests do the same through the autouse fixture in `src/tests/conftest.py`.

**What to look for.** `ow status` and `ow config` print every loader warning on stderr: untrusted, import failure, incompatible `OW_API_VERSION`, bad name, off-prefix setting. `ow files` shows what your `ow_generated_files` would write. `ow files --diff` shows the difference.

## Non-goals

- **No pip distribution in 3.0.** ow does not load plugins from installed packages (no entry points). A plugin lives in a repo of the workspace it applies to and is trusted by location. This is deliberate for 3.0.
- **No stability promise outside `ow.plugin_api`.**
- **No sandboxing of plugin code.** Trust is the only gate. A trusted plugin has your user's full rights.
