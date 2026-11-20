# Commands

Every ow command, its flags, its `--json` contract, and its exit codes.

## Exit codes

- **0** — the command succeeded, or nothing needed doing.
- **1** — a repo was refused, a step failed, or a plugin diagnostic blocked refresh.
- **2** — a usage error, a declined prompt, or a prompt with no terminal and no `-y`.
- **130** — interrupted.

## Shared options

- **`-w, --workspace WS`** — workspace to act on, by name or path (default: current directory's workspace).
- **`-t, --task TASK`** — logical task group linking flat workspaces (`add`, `status`, `ls`, `rm`).
- **`--only REPO`** — repeatable; limits `status` and opt-in `retarget` to named repos.
- **`-n, --dry-run`** — print the plan without running it (`repo rm`, `rm`, `prune`, `retarget`).
- **`-y, --yes`** — skip confirmation prompts (`repo rm`, `rm`, `prune`, `retarget`).
- **`--json`** — emit canonical `schema_version: 1` JSON on `stdout` and route all progress/plans to `stderr` (`add`, `status`, `ls`, `rm`, `prune`, `files`, `config`, `retarget`). Implies non-interactive mode.
- **`--offline`** — skip external network lookups (`status`, `prune`).

---

## Workspace creation & execution

### `ow add TARGET… [--name NAME] [-t TASK] [--base REF] [--branch BRANCH] [-r REPO…] [--json]`

Create one or more workspaces in the current directory.

- **PR targets** (via built-in `github` plugin): `enterprise#12345`, `odoo/iap-apps#42`, `odoo/odoo/pull/1234`, or a GitHub PR URL (`https://github.com/odoo/iap-apps/pull/1234`). Repo names resolve against registered bare repos (`ow repo ls`) before falling back to plugin known repos and `github.default_org`. Creates `<num>-<branch>/` by default, fetches `refs/pull/<num>/head`, discovers open sister PRs matching the same branch across known repos in the same org, and expands Odoo addon repos with `community` on the base branch.
- **Branch targets**: `<repo>@<branch>` (or `<repo>@<remote>/<branch>`) searches configured remotes of `<repo>` in parallel (falling back to local branches), checks out `<branch>` tracking the matching remote, and infers the Odoo series base when applicable.
- **Composite targets**: `iap-apps#42+enterprise` or `enterprise@18.0-my-fix+iap@18.0-iap-fix` combines multiple specs into a single workspace.
- **Multi-workspace tasks (Issue #84)**: `ow add -t voip-task iap-apps#42+enterprise enterprise#15402 voip#89` creates flat sibling workspaces, auto-allocates non-colliding `odoo.http_port` and `odoo.gevent_port` values, and writes `<common-parent>/voip-task.code-workspace`.
- **Fresh workspaces**: `ow add my-fix --base 18.0 [--branch 18.0-my-fix]` creates a workspace using `-r/--repo` (or `odoo.default_repos`, defaulting to `["community", "enterprise"]`).
- **Idempotency**: Re-running `ow add` on an existing (or interrupted) ow workspace keeps healthy worktrees intact, cleans up incomplete/broken worktrees, adds any missing repos, and refreshes generated files. Refuses if the target directory already exists and is not an ow workspace.

### `ow run [TARGET] [-w WS] [--tmp] [-- ARGS…]`

Run a workspace locally via its plugin runner (`odoo` launches `odoo-bin -c odoorc`, preferring the resolved virtualenv `$XDG_DATA_HOME/ow/venvs/py<ver>-<series>`, falling back to `mise exec` then `python3`).

- `TARGET` and `-w WS` are mutually exclusive; `--tmp` requires `TARGET`.
- Without `TARGET`: runs the current workspace (or `-w WS`). Everything after `--` (flags or subcommands like `ow run -- shell`) is forwarded verbatim to the runner.
- With `TARGET` (e.g. `ow run enterprise#12345 -- -i sale`): reuses an existing workspace of that name or creates it in `cwd`, then runs it.
- With `--tmp` / `--ephemeral`: creates a detached temporary workspace outside `cwd`, runs it, and removes its worktrees and directory on exit.

---

## Repos

### `ow repo add NAME [URL] [--base REF] [-w WS]`
Register a bare repo under `$XDG_DATA_HOME/ow/repos/<name>.git` and, inside a workspace, add its worktree. Known Odoo repos (`community`, `enterprise`, `design-themes`, `iap-apps`, `upgrade`, `upgrade-util`, `voip`) need no URL.

### `ow repo ls [-w WS]`
List registered bare repos and their `origin` URLs.

### `ow repo rm NAME [-n] [-y] [-w WS]`
Remove a repo's worktree from the workspace (refuses dirty worktrees or orphan commits; keeps unpushed local branches).

---

## Status, files & configuration

### `ow status [TARGET] [-w WS] [-t TASK] [--only REPO…] [--fetch] [--offline] [--json]`
Show each repo's branch, base, upstream, ahead/behind counts, dirty state, and external plugin status (GitHub PR state cached for `github.cache_ttl` seconds by default; pass `--offline` to skip network). `TARGET`, `-w`, and `-t` are mutually exclusive; `--only` applies to single-workspace status only, and `--fetch` cannot be combined with `TARGET`.

- `--fetch`: runs parallel surgical fetches of tracked refs before printing status.
- `-t TASK`: inspects every workspace in `TASK`.
- `--json`: includes sibling task workspaces, paths, ports, `.code-workspace` path, and PR metadata for external UIs and coding harnesses.
- `TARGET`: queries external status plugins directly (e.g. `ow status enterprise#12345`, or a Runbot bundle with a future `runbot` plugin).

### `ow refresh [-w WS]`
Recompute and write owned generated files (and sync `<task>.code-workspace` when in a task).

### `ow files [--diff] [--json] [-w WS]`
Show generated files and their ownership state (`absent`, `up to date`, `outdated`, `yours`, `skipped`), or unified diffs with `--diff`.

### `ow config [--global] [--json] [-w WS]`
### `ow config get KEY [--global] [--json] [-w WS]`
### `ow config set KEY VALUE… [--global] [-w WS]`
### `ow config unset KEY [--global] [-w WS]`
Read and write typed plugin settings across default, global (`$XDG_CONFIG_HOME/ow/config.toml`), and workspace (`.ow/config.toml`) layers.

---

## Fleet management

### `ow ls [-t TASK] [--json]`
List indexed workspaces, their paths, repos, and task membership.

### `ow cd [WS]`
Print a workspace's path (used by `ow shell-init`).

### `ow shell-init {bash|zsh|fish}`
Print the shell wrapper for `ow cd` and completion script.

### `ow rm {WS | -t TASK} [-n] [-y] [--json]`
Remove a workspace (or every workspace in `-t TASK`; pass exactly one of `WS` or `-t TASK`), its worktrees, and pushed branches. Refuses dirty worktrees, orphan commits, or stray checkouts (preflights all members before deleting any workspace in a task). Updates or removes `<task>.code-workspace`.

### `ow prune [--offline] [-n] [-y] [--json]`
Clean up prunable worktree registrations, pushed orphan branches, dead index entries, and (unless `--offline`) clean workspaces whose checked-out branches all belong to `MERGED` or `CLOSED` GitHub PRs.

---

## Opt-in plugins

### `ow retarget [BASE] [--base REF] [--autostash] [--no-fetch] [--only REPO…] [-n] [-y] [--json] [-w WS]`
Guarded multi-repo replay (`cherry-pick --ff`, `last_taken`, `--fork-point` fallback). `BASE` and `--base` are mutually exclusive. Hidden by default (`retarget.enabled = false`); enable globally with:

```sh
ow config set --global retarget.enabled true
```

A workspace can still opt out locally with `ow config set retarget.enabled false`.

---

## Confirmation rules & `--json` contract

- **Prompts**: Only mutating/destructive commands prompt (`repo rm`, `rm`, `prune`, and non-fast-forward `retarget`). Pass `-y` to confirm non-interactively or `-n` for a dry run.
- **`--json` envelope**: Supported on `add`, `status`, `ls`, `rm`, `prune`, `files`, `config`, `config get`, and `retarget`. Every JSON response is a UTF-8 JSON object on `stdout` carrying `"schema_version": 1`; all progress lines, plans, and warnings go to `stderr`, and interactive prompts are disabled (pass `-y` for prompting commands).
