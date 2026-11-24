# Writing a plugin

An ow plugin is a Python module whose `@hookimpl` functions ow calls at defined moments: to declare settings, generate files, resolve specs, run a workspace, report status, and so on. The ten built-in plugins (`mise`, `odoo`, `postgres`, `mailsink`, `github`, `runbot`, `vscode`, `zed`, `local`, `sandbox`) use the same hooks. You can ship a plugin **inside a repo** that a workspace checks out: ow finds it, asks you to trust it once, and loads it next to the built-ins.

Everything here is the **plugin API v1**, available since ow 3.0. Import only from `ow.plugin_api`.

- [A complete example](#a-complete-example)
- [Discovery](#discovery)
- [Trust](#trust)
- [Versioning and compatibility](#versioning-and-compatibility)
- [Settings](#settings)
- [Hook reference](#hook-reference)
- [Data records](#data-records)
- [Testing](#testing)
- [Non-goals](#non-goals)

## A complete example

A repo with this `ow_plugin.py` at its root (`hello`, below) adds a setting, a generated file, a status line and a runner:

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
ow trust                      # trusts the plugin file, then refreshes: "wrote HELLO.txt"
ow status                     # … "# hello: 1 repo(s)" and "# plugin: hello (…/hello/ow_plugin.py)"
ow config get hello.greeting  # hello
ow run -- world               # "# starting <ws> (plugin hello: …)" then "hello world"
```

Rules visible in the example:

- A hook implementation may declare **any subset** of the hook's arguments, by name (`ow_generated_files(ws)`, not the full `(ws, answers, multi)`). A parameter with a default value is accepted but never passed: it keeps its default.
- Returning `None` means "nothing to say".
- Hooks are plain module-level functions; the module itself is the plugin.

## Discovery

ow looks for a plugin in **each repo of a workspace** (every repo of every stack of a multi-stack workspace), in the repo's worktree:

1. `[tool.ow] plugin = "path/to/plugin.py"` in the repo's `pyproject.toml`, relative to the repo root; else
2. `ow_plugin.py` at the repo root.

One plugin per repo. A path that is absolute, escapes the repo (`..`, symlinks) or does not exist is ignored. The module must define:

| Name | Required | Meaning |
|---|---|---|
| `OW_API_VERSION` | yes | Integer API version the plugin was written for: `1` |
| `PLUGIN_NAME` | no | The plugin's name, which prefixes its settings and its `[<name>]` config table. Matches `[A-Za-z0-9_-]+`; defaults to the repo's name in the workspace |

A plugin is loaded after the built-ins, so it comes last in plugin order. In a multi-stack workspace a trusted plugin from one stack is loaded for every stack of that workspace. Like any plugin it can be switched off with `<name>.enabled = false` (see `ow config`).

The plugin is a single file loaded by path: its directory is **not** put on `sys.path`, so a sibling module cannot be imported; keep it in one file. ow imports it without writing `__pycache__` into your repo. If the import fails (a syntax error, an exception, a missing import) ow prints `warning: skipping plugin <path>: failed to import (<error>)` and carries on without it; same for an invalid or already registered name. `ow status` and `ow run` name the loaded in-repo plugins (`# plugin: hello (<path>)`).

## Trust

A plugin is arbitrary Python code that runs with your user's rights every time ow opens the workspace. ow therefore runs an in-repo plugin only from a **trusted location**. Trust works like an editor's workspace trust: it is granted to a *location*, not to a file's content.

- An untrusted plugin is skipped with `warning: untrusted plugin <path>; run `ow trust` to enable it`. A command never prompts in the middle of its run.
- `ow trust PATH` trusts a plugin file or a **directory**: a trusted directory covers every plugin beneath it.
- `ow trust` with no `PATH` trusts the plugins of the current workspace and refreshes it. On a terminal it asks what to trust: only the plugin files found (the default), everything in the workspace root, or everything in the workspace's parent directory (for example `~/Odoo`). Without a terminal it trusts the plugin files found.
- `ow trust --list` shows the trusted locations; `ow trust --untrust [PATH]` revokes one. Revoking a file that a trusted directory still covers leaves it trusted, and says so.
- The registry is `$XDG_STATE_HOME/ow/trusted-plugins.toml` (mode `0600`), recording canonical paths.

> **Trusting a directory trusts everything that ever shows up under it.** ow does not look at what is in the file: a trusted plugin stays trusted after you edit it, after you switch branch, and after `ow add enterprise#12345` checks out a pull request that adds or modifies an `ow_plugin.py` — that code runs on the next ow command, with no prompt. Trust a plugin file for a repo you control; trust a directory only when everything that can land under it is code you would run anyway.

## Versioning and compatibility

- A plugin declares `OW_API_VERSION = 1`. ow loads a plugin whose version is an integer inside `ow.plugin_api.SUPPORTED_API_RANGE` (currently `(1, 1)`); any other value, or none, skips the plugin with `warning: skipping plugin <path>: incompatible OW_API_VERSION=<value> (ow supports v1)`. The command still succeeds.
- **API v1 is frozen through every 3.x release.** 3.x only *adds*: new hooks, new optional hook arguments, new optional fields with defaults, new exports. Nothing a v1 plugin relies on is renamed or removed in 3.x.
- Anything slated for removal first emits a deprecation warning in a 3.x release and is removed in 4.0, which raises `OW_API_VERSION`.
- Everything in this page's [hook reference](#hook-reference) and [data records](#data-records) is public API since 3.0. Anything not exported from `ow.plugin_api` (`ow.core.*`, `ow.specs.*`, `ow.plugins.*`, `ow.commands.*`) is internal and may change in any release.

## Settings

A plugin declares settings from `ow_settings` and reads them from `ws.settings`:

```python
Setting(key="hello.greeting", type=str, default="hello", help="…", unique=False)
```

- **Prefix.** Every key must start with `<plugin name>.`. A setting outside the prefix, or whose key is already declared, is dropped with `warning: plugin hello: ignoring setting 'odoo.greeting' (keys must start with 'hello.')`; the plugin still loads. A plugin can never shadow another plugin's setting.
- **`enabled`.** ow synthesizes `<plugin name>.enabled` (bool, default `true`) for every plugin; do not declare it. When it is `false` for a workspace, none of the plugin's hooks run for it.
- **Types.** `str`, `int`, `bool` or `list`. A config value of the wrong type keeps the default and warns.
- **Default.** A value, or a function of the `Workspace` (`odoo.db_name` defaults to the workspace name); the function's result is `None` where there is no workspace.
- **`unique=True`** (only meaningful for `str`): the value must differ across workspaces. When ow creates a workspace or stack whose value is still the default and equals the value another indexed workspace resolves to, it writes `<value>-2` (next free suffix) into the new workspace's config. A value the user set is never changed, and a workspace that lacks your in-repo plugin does not count.
- **Explicit vs default.** `ws.is_explicit("hello.greeting")` is true when any config layer set the key; `ws.origins` maps each explicit key to its layer (`global`, `workspace`, `stack`). A plugin that takes values from a service must only fill the settings the user left at their default, so an explicit value, even one equal to the default, always wins.

Users set them with `ow config set hello.greeting hi` ([Configuration](configuration.md)).

## Hook reference

All hooks are public since 3.0. Plugins run in plugin order: built-ins first (`mise`, `odoo`, `postgres`, `mailsink`, `github`, `runbot`, `vscode`, `zed`, `local`, `sandbox`), then in-repo plugins. A plugin is skipped for a workspace where `<name>.enabled` is false.

**Call style**

- **call** — every enabled plugin is called, in plugin order; `None` answers are dropped and the others are gathered.
- **first** — plugins are called in order until one answers non-`None`; that answer wins and later plugins are not called.
- **pipe** — one value is threaded through every plugin in order; each non-`None` return replaces it for the next.
- **answers** — the hook is not called by the core directly: a consumer plugin, or the core's merge of the shared specs, asks `answers("hook_name")` and gets every enabled plugin's answer for one workspace.

**Error policy**

- **Propagates** — an exception aborts the command as `plugin <name> failed in <hook>: …` (exit 1). A `UsageError` raised by the hook keeps its own exit code (2).
- **Collected** — an exception becomes a warning naming the plugin and the command continues with the other plugins' answers.

**Arguments.** `ws` is the `Workspace` (a workspace or one stack), `multi` a `MultiWorkspace` when the command runs at a multi-stack root. `answers` is injected when you declare it (and a workspace is known): `answers("python_environment")` returns the list of every enabled plugin's answer for that workspace, and `answers.for_workspace(other_ws)` binds it to another workspace of the same session, such as a sibling stack. The one hook that gets `answers` without a `ws` is `ow_multi_workspace_files`: its `answers` is bound to the first stack of `multi`, so call `answers.for_workspace(stack)` to ask about another one; it is not called at all for a root without stacks.

### Core hooks

Every row is *since 3.0*.

| Hook → returns | Called when | Style | Errors |
|---|---|---|---|
| `ow_settings()` → `list[Setting]` | When ow collects the plugins' settings (loading, `ow config`, every command) | call | Propagates for a built-in plugin; collected for an in-repo plugin: that plugin alone is skipped with a warning and the others still load |
| `ow_generated_files(ws, answers, multi)` → `list[GeneratedFile \| Diagnostic]` | Every refresh of a workspace or stack | call | Propagates |
| `ow_diagnostics(ws, multi)` → `list[Diagnostic]` | Opening a session and every refresh | call | Collected |
| `ow_known_repos()` → `list[KnownRepo]` | Resolving a repo name or spec, `ow repo add`, completion | call | Propagates |
| `ow_workspace_created(ws)` → `None` | After `ow init`, `ow add` or `ow run --tmp` creates a workspace or stack | call | Propagates |
| `ow_refreshed(ws, owned)` → `None` | After a refresh; `owned` is every path ow wrote or found up to date | call | Propagates |
| `ow_resolve_spec(spec, known_repos, settings)` → `ResolvedSpec \| None` | Resolving each item of a spec (`enterprise#123`, `@18.0`, a URL) | first | Propagates |
| `ow_expand_workspace_repos(repos, known_repos, settings)` → `tuple[ResolvedRepo, ...] \| None` | After resolving, to add companion repos (the Odoo core next to an addon PR) | pipe | Propagates |
| `ow_runner(ws, args, detach, answers)` → `RunCommand \| None` | `ow run` of one workspace or stack; `args` are the extra arguments | first | Propagates |
| `ow_running(ws)` → `list[str] \| None` | Asking whether a plugin-managed process runs: `ow run` (refuse a double start), `ow stop`, `ow logs -f`, `ow rm`, `ow prune` | call | Collected (a warning naming the plugin; it counts as not running) |
| `ow_stop(ws)` → `RunCommand \| list[str] \| None` | `ow stop WS`; a `RunCommand` is executed, a list of lines is printed | first | Propagates |
| `ow_stop_services(ws)` → `list[str] \| None` | `ow stop --services` / `--all`; `ws` is `None` | call | Collected |
| `ow_logs(ws)` → `list[tuple[str, Path]] \| None` | `ow logs`; `(label, log_path)` pairs are shown and followed | first | Propagates |
| `ow_before_run(ws, workspaces)` → `list[str] \| None` | Before `ow run` and `ow sandbox` start; prepare shared services, return info lines | call | Collected (warns, the run goes on) |
| `ow_reset_preview(ws, answers)` → `list[str] \| None` | `ow run --reset`, before the confirmation: one line per thing `ow_reset_run` will remove | call | Propagates (a failing preview aborts `--reset`) |
| `ow_reset_run(ws, answers)` → `list[str] \| None` | `ow run --reset`, once confirmed; remove local runtime state, return info lines. A plugin that implements it without `ow_reset_preview` is listed as `<plugin>: local state` in the confirmation | call | Propagates |
| `ow_status(ws, facts, spec, offline, known_repos, settings)` → `list[StatusInfo]` | `ow status`; `ws` is `None` and `spec` set for `ow status SPEC` | call | Collected (contributes nothing) |
| `ow_prune_candidates(workspaces, offline)` → `list[PruneCandidate]` | `ow prune` | call | Collected |
| `ow_stack_values(ws, ws_values, siblings, pending, global_settings, repo_paths, repo_names)` → `dict[str, object] \| None` | Creating or promoting a stack: settings to pin explicitly (coordinated ports, database name) | call | Propagates |
| `ow_stack_name(ws)` → `str \| None` | Promotion to multi-stack: suggest the stack directory name | first | Propagates |
| `ow_stack_info(ws)` → `StackInfo \| None` | Showing a stack (`ow status`, `ow run`, `ow init`/`add`): ports and a note | call (merged) | Propagates |
| `ow_is_base_branch(repo, branch)` → `bool \| None` | Deciding whether a branch is an upstream series (checked out detached) | first | Propagates |
| `ow_sandbox_mounts(ws, answers)` → `SandboxMounts \| None` | `ow sandbox`: extra mounts and variables | call (merged) | Propagates |
| `ow_multi_workspace_files(multi, answers)` → `list[GeneratedFile \| Diagnostic]` | Every refresh of a multi-stack root: files written at the root; `answers` is bound to the first stack | call | Propagates |
| `ow_multi_runner(multi, args, detach)` → `RunCommand \| None` | `ow run` at a multi-stack root: one command for the whole workspace, instead of one per stack | first | Propagates |
| `ow_multi_stop(multi)` → `RunCommand \| list[str] \| None` | `ow stop` at a multi-stack root | first | Propagates |
| `ow_multi_logs(multi)` → `list[tuple[str, Path]] \| None` | `ow logs` at a multi-stack root | first | Propagates |

Details worth knowing:

- **`ow_generated_files`** returns `GeneratedFile`s (path relative to the workspace root, bytes, mode) and optionally `Diagnostic`s. Ownership, the lock and "never overwrite a hand edit" are ow's job ([Generated files](files.md)). Two plugins producing one path, an absolute path, a path escaping the workspace, or `.ow/config.toml` and the lock are errors. A `Diagnostic` of level `error` blocks the whole refresh (nothing is written, exit 1); `warning` is printed; `info` shows in `ow status` as `# <plugin>: <text>`.
- **`ow_status`** returns `StatusInfo(plugin=…, scope=…, subject=…, summary=…)` with `scope` one of `workspace`, `repo`, `external` or `service`. Network lookups must honour `offline`.
- **`ow_stack_values`** answers are merged in plugin order: a key already present (the user's own, or an earlier plugin's) is never overridden. **`ow_stack_info`** answers are merged: `ports` are combined, `note`s joined with `, `.
- **`ow_runner`** may return a `RunCommand` with `self_detaching=True` when the command itself starts background services and exits (`ow run -d` then does not detach it again). `ow_stop` / `ow_multi_stop` give such plugins the matching `ow stop`. A `RunCommand`'s exit code becomes ow's exit code. `ow_logs` / `ow_multi_logs` return log files only: ow shows or follows them for `-f` and `-n`, which a command could not honour. An empty list means "nothing from this plugin", like `None`: `ow logs` then shows `.ow/logs`.
- **`ow_resolve_spec`** returns `ResolvedSpec(name, repos, source)` for a spec it recognises and `None` for the rest; `ow_expand_workspace_repos` receives and returns the tuple of `ResolvedRepo`s.
- **`ow_multi_*`** hooks have no `ws`. If no plugin answers `ow_multi_runner` or `ow_multi_stop`, ow calls `ow_runner` / `ow_stop` stack by stack; if none answers `ow_multi_logs`, `ow logs` shows the stacks' own `.ow/logs` files.
- **A `RunCommand` runs in the foreground with ow's terminal**, and Ctrl+C reaches it too (same process group). The first Ctrl+C only makes ow keep waiting, so your command can tear down; a second one terminates it (SIGTERM, then SIGKILL after a few seconds). An interrupted run exits 130. `RunCommand.env` is laid over ow's own environment (`os.environ`) and nothing else: the `env` of `python_environment` answers is **not** applied, so put what the command needs in `RunCommand.env`.
- **Files a plugin generates inside a repo's worktree** (the user's checkout) must be gitignored, or git reports them as untracked. If a tool of the repo rewrites one, it no longer matches ow's bytes and becomes `yours` ([Generated files](files.md)): ow leaves it alone until it is deleted.

### Shared specs

Four more hooks let plugins cooperate without knowing each other. They are *since 3.0*, all **answers**-style (consumers call `answers("<name>")`), and an exception propagates.

| Hook → returns | Consumed by | Merge |
|---|---|---|
| `python_environment(ws)` → `PythonEnvironment \| None` | Environment backend (`mise`), `ow run`, `ow exec`, `resolve_stack_python` | `path`, `requirements`, `postinstall` concatenated; `env`, `env_paths`, `aliases` later answer wins per key; `python` and `venv_key` later non-`None` wins |
| `editors_contribution(ws, answers)` → `EditorContribution \| None` | `vscode`, `zed` | `launches` concatenated; `excludes` concatenated and deduplicated; `language_servers` merged per server and key, later wins; `python` later non-`None` wins |
| `database_service(ws)` → `DatabaseService \| None` | `odoo` (fills `odoo.db_*` left at default) | the consumer decides; `odoo` takes the first |
| `smtp_service(ws)` → `SmtpService \| None` | `odoo` (fills `odoo.smtp_*` left at default) | the consumer decides; `odoo` takes the first |

`PythonEnvironment` paths are relative to the workspace root. The virtualenv is computed once, from the merged answers; `resolve_stack_python(ws, answers)` gives a stack's interpreter path (`answers` is the one ow hands to your hook; `ws` may be a sibling stack).

## Data records

Exported by `ow.plugin_api`, all frozen dataclasses with **keyword-only** fields: build them as `Setting(key=…, type=…)`, never positionally, so ow can add fields in any 3.x release. Fields are listed in declaration order; `= x` is a default:

| Record | Fields |
|---|---|
| `Setting` | `key`, `type`, `default`, `help`, `unique = False` |
| `GeneratedFile` | `path` (a `str` or `PurePosixPath`, relative), `data: bytes`, `mode = 0o644` |
| `Diagnostic` | `level` (`"info"`, `"warning"` or `"error"`), `text` |
| `KnownRepo` | `name`, `url` |
| `ResolvedRepo` | `name`, `url = None`, `branch = None`, `base = None`, `remote = None`, `fetch_ref = None`, `commit = None`, `explicit = True`, `pr = None` |
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
| `WorkspaceVenv` | `python_version`, `path`, `diagnostics = ()`; `is_local(ws)` |

What plugins receive (read-only, also keyword-only dataclasses):

| Record | Fields |
|---|---|
| `Workspace` | `root: Path`, `name`, `repos: tuple[Repo, ...]`, `settings` (mapping), `is_stack`, `origins`; `is_explicit(key)` |
| `Repo` | `name`, `path` (the worktree), `branch` (`None` = detached), `base` |
| `MultiWorkspace` | `root`, `name`, `stacks: tuple[Workspace, ...]` |
| `RepoFacts` | `name`, `path`, `bare`, `remotes`, `head`, `branch`, `upstream`, `upstream_ab`, `base`, `default_base`, `explicit_base`, `operation`, `modified`, `untracked`, `problem` |
| `Answers` | callable `answers("hook")` → `list`; `for_workspace(ws)` |

Errors: `UsageError` (exit 2: a mistake the user can fix) and `PluginError` (exit 1). Raise `UsageError("message")` for a clean one-line error; any other exception is reported as a `PluginError` naming your plugin and the hook.

Helpers: `hookimpl`, `PLUGIN_API_VERSION`, `SUPPORTED_API_RANGE`, `resolve_stack_python(ws, answers)`, `workspace_venv(ws, env)`, `detect_container_engine()` (`docker`/`podman`/`None`), `is_port_free(port, host="127.0.0.1")`, `follow_log_files(entries, *, lines, follow, running)` (the log viewer `ow logs` uses). The package ships `py.typed`, so type checkers see these annotations.

## Testing

**Unit.** A hook is a plain function: import the plugin file and call it with a hand-built `Workspace`. Only `ow` itself needs to be importable.

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

**End to end, without touching your machine.** Give ow a throwaway home and a local remote; this is how the example above was checked:

```sh
export HOME=/tmp/play/home XDG_CONFIG_HOME=/tmp/play/home/.config XDG_DATA_HOME=/tmp/play/home/.local/share \
       XDG_STATE_HOME=/tmp/play/home/.local/state XDG_CACHE_HOME=/tmp/play/home/.cache
export GIT_CONFIG_GLOBAL=/tmp/play/gitconfig           # [protocol "file"] allow = always, user.name/email
export OW_CONTAINER_ENGINE=none                          # a name that is not installed: `ow run` finds no container engine
mkdir -p /tmp/play/ws && cd /tmp/play/ws
ow repo add hello file:///path/to/hello-repo
ow init hello && ow trust && ow status && ow config
```

The trust registry lives under `XDG_STATE_HOME`, so trusting in the throwaway home does not trust anything in your real one. ow's own tests do the same through the autouse fixture in `src/tests/conftest.py`.

**What to look for.** `ow status` and `ow config` print every loader warning (untrusted, import failure, incompatible `OW_API_VERSION`, bad name, off-prefix setting) on stderr; `ow files` shows what your `ow_generated_files` would write; `ow files --diff` shows the difference.

## Non-goals

- **No pip distribution in 3.0.** ow does not load plugins from installed packages (no entry points, no `ow.plugins` group): a plugin lives in a repo of the workspace it applies to, and is trusted by location. This is deliberate for 3.0, not an omission.
- **No stability promise outside `ow.plugin_api`.**
- **No sandboxing of plugin code.** Trust is the only gate: a trusted plugin has your user's full rights.
