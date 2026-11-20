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

- **PR targets** (via built-in `github` plugin): `enterprise#12345`, `odoo/iap-apps#42`, or a GitHub PR URL. Creates `<num>-<branch>/` by default, fetches `refs/pull/<num>/head`, discovers open sister PRs matching the same branch across known repos in the same org, and expands Odoo addon repos with `community` on the base branch.
- **Composite targets**: `iap-apps#42+enterprise` combines multiple specs into a single workspace.
- **Multi-workspace tasks (Issue #84)**: `ow add -t voip-task iap-apps#42+enterprise enterprise#15402 voip#89` creates flat sibling workspaces, auto-allocates non-colliding `odoo.http_port` values, and writes `<common-parent>/voip-task.code-workspace`.
- **Fresh workspaces**: `ow add my-fix --base 18.0 [--branch 18.0-my-fix]` creates a workspace using `-r/--repo` (or `odoo.default_repos`, defaulting to `["community", "enterprise"]`). Refuses if the target directory already exists.

### `ow run [TARGET] [-w WS] [--tmp] [-- ARGS…]`

Run a workspace locally via its plugin runner (`odoo` launches `odoo-bin -c odoorc` using the shared virtualenv `$XDG_DATA_HOME/ow/venvs/py<ver>-<series>`).

- Without `TARGET`: runs the current workspace (or `-w WS`).
- With `TARGET` (e.g. `ow run enterprise#12345 -- -i sale`): reuses an existing indexed workspace of that name or creates it in `cwd`, then runs it.
- With `--tmp` / `--ephemeral`: creates a temporary workspace outside `cwd`, runs it, and removes its worktrees and directory on exit.

---

## Repos

### `ow repo add NAME [URL] [--base REF] [-w WS]`
Register a bare repo under `$XDG_DATA_HOME/ow/repos/<name>.git` and, inside a workspace, add its worktree. Known Odoo repos (`community` with alias `odoo`, `enterprise`, `design-themes`, `iap-apps`, `upgrade`, `upgrade-util`, `voip`) need no URL.

### `ow repo ls [-w WS]`
List registered bare repos and their `origin` URLs.

### `ow repo rm NAME [-n] [-y] [-w WS]`
Remove a repo's worktree from the workspace (refuses dirty worktrees or orphan commits; keeps unpushed local branches).

---

## Status, files & configuration

### `ow status [TARGET] [-w WS] [-t TASK] [--only REPO…] [--fetch] [--offline] [--json]`
Show each repo's branch, base, upstream, ahead/behind counts, dirty state, and external plugin status (GitHub PR state cached for `github.cache_ttl` seconds by default; pass `--offline` to skip network).

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

### `ow rm [WS] [-t TASK] [-n] [-y] [--json]`
Remove a workspace (or every workspace in `-t TASK`), its worktrees, and pushed branches. Refuses dirty worktrees, orphan commits, or stray checkouts. Updates or removes `<task>.code-workspace`.

### `ow prune [--offline] [-n] [-y] [--json]`
Clean up prunable worktree registrations, pushed orphan branches, dead index entries, and (unless `--offline`) clean workspaces whose tracked GitHub PRs are `MERGED` or `CLOSED`.

---

## Opt-in plugins

### `ow retarget [BASE] [--base REF] [--autostash] [--no-fetch] [--only REPO…] [-n] [-y] [--json] [-w WS]`
Guarded multi-repo replay (`cherry-pick --ff`, `last_taken`, `--fork-point` fallback). Hidden by default (`retarget.enabled = false`); enable globally with:

```sh
ow config set --global retarget.enabled true
```
