# Commands

The single reference for every `ow` command: synopsis, flags, exit codes and confirmation rules. `ow --help` and `ow <command> --help` print the same synopses.

## Naming

- **`WS`** — a workspace, by name or path. Where a stack is accepted, `WS[/STACK]`: `review` is a workspace, `review/iap` one of its stacks. Resolution order: a path (`./iap` inside a multi-stack root wins), `<workspace>/<stack>`, an indexed workspace name, then a stack name that is unique across indexed multi-stack workspaces. An ambiguous name is a usage error listing the matches.
- **`STACK`** — the name of a stack inside a multi-stack workspace (`--as STACK`, `[stacks.<stack>]`).
- **`SPEC`** — what to check out, see [Specs](#specs): `enterprise#12345`, `@18.0`, a Runbot URL, `iap=iap#4321`.

## Exit codes

- **0** — the command succeeded, or nothing needed doing.
- **1** — a repo was refused, a step failed, a plugin hook failed, or a plugin diagnostic blocked a refresh. `ow logs` with no log to show also exits 1.
- **2** — a usage error, a declined prompt, or a prompt with no terminal and no `-y`.
- **130** — interrupted with Ctrl+C.
- **The child's code** — `ow run`, `ow exec`, `ow sandbox`, and `ow logs`/`ow stop` when a plugin supplies the command, pass the exit code of the process they ran through unchanged; death by signal `n` is `128 + n`. A `1` or `2` from these commands may therefore be the child's, not ow's. At a multi-stack root `ow run` returns the code of the first stack (in stack order) that exited non-zero.

## Shared options

- **`-w, --workspace WS`** — workspace or stack to act on (default: the current directory's). Mutually exclusive with a positional `WS`/`SPEC`/`PATH`.
- **`--only REPO`** — repeatable; limits `status` (one workspace or stack) and `retarget` to the named repos.
- **`-n, --dry-run`** — print the plan without running it (`init`, `add`, `sandbox`, `repo rm`, `rm`, `prune`, `retarget`).
- **`-y, --yes`** — skip confirmation prompts (`init`/`add` promotion, `repo rm`, `rm`, `prune`, `retarget`, `run --reset`).
- **`--offline`** — skip external network lookups (`status`, `prune`).

---

## Specs

`ow init`, `ow add` and `ow run --tmp` (`SPEC`) share one grammar:

```
[<stack>=]<item>[,<item>...]
```

Items within a stack are joined with a comma `,`. Each `<item>` takes one of five forms:

1. GitHub PR (`github` plugin): `enterprise#12345`, `odoo/iap-apps#42`, `enterprise/pull/12345`, or a PR URL.
   - Fetches `refs/pull/<num>/head`.
   - Discovers open sister PRs on the same branch across known repos in the same org.
   - Expands Odoo addon repos with `community`, detached on the base series.
   - Pins companion detached repos to Runbot's exact `base_match` SHA when available, and prints the pinned SHA, or a fallback note if Runbot is unreachable.
   - Pinned repos can later be moved onto a series tip with `ow retarget`.
2. Runbot bundle or batch URL (`runbot` plugin): `https://runbot.odoo.com/runbot/bundle/<id_or_name>` or `.../batch/<id>`. Checks out exact batch commits (detached), filtered to `(registered local bare repos ∪ odoo.default_repos)` so unregistered repos are never cloned implicitly.
3. `<repo>@<ref>` (or `<repo>@<remote>/<ref>`): resolves `<ref>` across configured remotes (preferring `origin` on tie, falling back to local branches). Upstream base/series refs (`master`, `main`, `18.0`, `saas-*`, `origin/HEAD`) check out detached to avoid cross-worktree branch collisions; feature branches check out as local tracking branches and infer their Odoo series base when applicable.
4. `@<base>`: expands `odoo.default_repos` (default `["community", "enterprise"]`) detached on `<base>`.
5. `<repo>`: a registered or known repo (e.g. `voip`, `upgrade`) detached on the stack's shared base if `origin` has it, otherwise `origin/HEAD`.

When `<stack>=` is omitted in a multi-stack context, `ow` derives the stack name from the spec (PR branch name, repo name, or base name) and deduplicates collisions (`-2`, `-3`). Plugins resolve PRs and URLs (`ow_resolve_spec`, see [Plugins](plugins.md)).

---

## Workspace initialization, evolution & execution

### `ow init [SPEC…] [--branch BRANCH] [--as STACK] [-n] [-y]`

Initialize, compose, adopt, or repair the current directory (`.`) in place.

- 0 specs in an empty directory: creates an empty `.ow/config.toml` and registers `.` in the workspace index.
- `--branch BRANCH`: creates or checks out `BRANCH` in every added repo.
- 1 unnamed spec (e.g. `ow init enterprise#12345`, `ow init @18.0 --branch 18.0-fix`): initializes a mono-stack workspace directly in `.`.
- 1+ named specs or 2+ specs (e.g. `ow init odoo=community#56789,enterprise#12345 iap=iap#4321 voip=voip`): initializes a multi-stack workspace with a single root `.ow/config.toml` declaring `[stacks.<stack>]` (no `.ow/` inside stack directories), auto-allocated non-colliding `odoo.http_port` / `odoo.gevent_port` on Odoo stacks, and a root `<workspace>.code-workspace`.
- A new workspace whose `unique` setting (`odoo.db_name`) is still at its default and equals another indexed workspace's gets `<value>-2` (next free suffix) written to its config.
- Re-running `ow init` inside an existing or moved workspace repairs worktrees (`git worktree repair`), updates the workspace index, refreshes generated files (and `<workspace>.code-workspace`), and applies any additional `SPEC` arguments.
- Refuses to initialize a parent directory that already contains standalone (non-stack) ow workspaces.

### `ow add SPEC… [--branch BRANCH] [--as STACK] [-n] [-y]`

Evolve an already-initialized workspace (refuses outside a workspace with a hint to use `ow init` or `ow run --tmp`).

- Inside a mono-stack workspace (or a stack):
  - Unnamed specs add missing repos or attach a PR / feature branch onto an existing clean/detached worktree (refusing dirty worktrees, orphan commits, or unpushed work).
  - Named specs (`<stack>=<spec>`) inside a mono-stack workspace promote it to multi-stack after confirmation (pass `-y` to skip prompt, `-n` to preview): moves existing worktrees and `.ow/` into `<existing-name>/` via staging, repairs git worktrees, preserves `odoo.db_name` and ports, creates the new stack(s), and writes `<workspace>.code-workspace`. Use `--as STACK` to pick the existing stack's name explicitly.
- At a multi-stack workspace root:
  - `<existing-stack>=<spec>` adds repos or attaches PRs into that existing stack.
  - `<new-stack>=<spec>` or an unnamed `<spec>` creates a new stack with coordinated ports and updates `<workspace>.code-workspace`.

### `ow run [WS…] [-w WS] [-d] [--tmp] [--fail-fast] [--reset] [-y] [-- ARGS…]`

Run one or more stacks locally via their plugin runner (`odoo` launches `odoo-bin -c odoorc`, preferring `$XDG_DATA_HOME/ow/venvs/py<ver>-<series>`, falling back to `mise exec` then `python3`). Before starting, `ow_before_run` starts the shared services (`ow-postgres`, `ow-pgweb`, `ow-mailsink`; see [Services](migrating-to-3.0.md#services)) and generated files that depend on them are refreshed.

- Single stack / mono-stack workspace: inherits the terminal TTY directly so interactive tools (`pdb`, `ow run -- shell`) work natively.
- Multi-stack root (`ow run` with no `WS`, or several): runs all runnable stacks in parallel with `[stack]`-prefixed stdout/stderr (skipping non-runnable stacks such as `voip` with a note) and terminates process groups cleanly on `Ctrl+C`. Prints a `# summary:` line if any stack exits non-zero; pass `--fail-fast` to stop remaining stacks as soon as one exits non-zero.
- `-d, --detach`: starts the workspace or stack(s) in the background (recording PID metadata under `<root>/.ow/run/` and output under `<root>/.ow/logs/`, or delegating to a plugin's self-detaching startup such as `voip`'s `dev up`). Each detached start truncates that stack's log. Refuses if already running (pointing to `ow stop` / `ow logs -f`) and cannot be combined with `--tmp`.
- `--reset`: before starting each launched stack, shows what it will drop — `database <user>@<host>:<port>/<db>` and the local filestore (`.odoo/filestore/<db>`), plus `<plugin>: local state` for a plugin that resets something without describing it — and asks `Drop it?`. `-y/--yes` skips the question; without a terminal and without `-y` it is a usage error (exit 2, nothing dropped), as is answering no. The drop uses the managed `postgres` container (`DROP DATABASE IF EXISTS ... WITH (FORCE)`), or `psql`/`dropdb` on a custom host. The system databases (`postgres`, `template0`, `template1`) are refused.
- Per-stack arguments: pass `WS:"ARGS"` (parsed with `shlex.split`, splitting on the first `:` outside `://` and `git@host:`), e.g.:
  ```sh
  ow run odoo:"-i voip --dev=all" iap:"--log-level=debug"
  ```
  Shared `-- ARGS…` are appended to the launched stack; they cannot be combined with more than one `WS`. At a multi-stack root, `ow run -- ARGS` is refused when several stacks are runnable: name the stack.
- No silent creation: without `--tmp`, `WS` resolves only existing workspaces/stacks.
- Ephemeral runs (`--tmp` / `--ephemeral`, at least one `SPEC`): creates temporary mono-stack or multi-stack checkouts in a temp directory with non-colliding ports, runs them, and cleans up worktrees and temp directories on exit:
  ```sh
  ow run --tmp enterprise#12345 -- -i sale
  ow run --tmp odoo=enterprise#12345:"-i voip" iap=iap#4321
  ```

### `ow stop [WS…] [-w WS] [--services] [--all]`

Stop running detached workspaces/stacks and/or the shared service containers.

- Without `--services` or `--all`: stops only the named workspace/stack(s) (SIGTERM → SIGKILL on detached process groups, or the plugin's `ow_stop`/`ow_multi_stop` hook), leaving the shared containers running.
- `--services`: stops the shared service containers (`ow-postgres`, `ow-pgweb`, `ow-mailsink`). Their data survives.
- `--all`: stops every running indexed workspace and all shared services; cannot be combined with `WS` or `-w`.

### `ow logs [WS…] [-w WS] [-f] [-n LINES]`

Print the last `LINES` lines (default 50) of the logs of a detached workspace or stack (`<root>/.ow/logs/<stack>.log`, or the plugin's `ow_logs`/`ow_multi_logs` hook such as `voip`'s `dev logs`). Several logs are interleaved chronologically, each line prefixed with `[stack]`.

- `-f, --follow`: keeps following new output until the followed process stops (a last read picks up what it wrote on exit) or you press Ctrl+C (exit 130).
- Exits 1 with `no logs found; start the workspace with ow run -d first` when there is nothing to show. An existing log of a stopped workspace is still printed (exit 0).
- Logs are never rotated; the next `ow run -d` starts a fresh one.

### `ow exec [WS] [-w WS] [-- COMMAND [ARGS…]]`

Run a command (e.g. `pip install phonenumbers`, `pytest`, `odoo-bin shell`) or an interactive shell (when `COMMAND` is omitted) inside a workspace or stack's resolved Python virtualenv and plugin environment. The command's exit code is `ow exec`'s.

- **Strict `--` separator**: anything before `--` is an optional workspace or stack (`WS`, mutually exclusive with `-w/--workspace`), and anything after `--` is executed as `COMMAND [ARGS…]`.
  ```sh
  ow exec -- pip install phonenumbers        # current workspace/stack
  ow exec iap -- pip install phonenumbers    # a stack from a multi-stack root
  ow exec iap                                # interactive shell in stack `iap`
  ```
- **Multi-stack root**: without `WS`/`-w`, auto-selects the single stack with a Python environment (e.g. `odoo/` alongside `voip/`); refuses when multiple Python stacks exist and lists them.
- **Environment & `cwd`**: sets `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `OW_WORKSPACE`, clears `PYTHONHOME`, prepends `<venv>/bin` and plugin `PATH` entries (such as `<ws>/community` for `odoo-bin`), and injects plugin environment variables (such as `ODOO_RC`). Preserves the current directory when already inside the stack, otherwise changes to its root.
- **Bootstrap & non-Python stacks**: for Python stacks, shares `ow run`'s virtualenv preparation (creating the venv via `mise` and installing requirements under a sidecar lock when needed, routing bootstrap output to `stderr` so `stdout` stays clean for pipes) and never falls back to system Python when the configured venv is missing. For non-Python stacks (e.g. `voipstack`), runs directly in the stack directory (via `mise exec --` when `mise` is available). Never starts the shared services. In interactive shell mode, prints `# <ws>: venv <path> (shared|local)` (or `mise`/`shell`) to `stderr`.

### `ow sandbox [-w WS] [--profile NAME] [--ro PATH…] [--rw PATH…] [--env VAR…] [-n] [-- COMMAND [ARGS…]]`

Run an arbitrary command, AI agent harness (`claude`, `opencode`, `codex`, `pi`, `omp`), or interactive shell (when `COMMAND` is omitted) inside a Linux bubblewrap (`bwrap`) sandbox scoped to a workspace, stack, or multi-stack root. The command's exit code is `ow sandbox`'s.

- **Filesystem isolation**: `$HOME` is an empty `tmpfs` (allowlist-only), system directories (`/usr`, `/etc`, `/opt`, …) are read-only, and only the workspace root (or multi-stack root) is mounted read-write. Each stack's `.ow/` directory and each repo's `.git` pointer file are overlaid read-only so the sandboxed process cannot alter workspace configuration or replace git pointers.
- **Shared bare repos (partial RW)**: only bare repos referenced by the workspace or stack(s) are mounted; each bare root, `config`, `hooks/`, and sibling workspaces' `worktrees/*` stay read-only while `objects/`, `refs/`, `logs/`, and this workspace's `worktrees/<id>/` are read-write — enabling `git add`/`commit`/`stash`/`rebase` without exposing hook/config sandbox-escape vectors. `~/.gitconfig` (and `~/.config/git/{config,ignore,attributes}`, never `credentials`) is mounted read-only.
- **Environment & harness profiles**: `bwrap --clearenv` starts from a strict allowlist (`HOME`, `USER`, `PATH`, `TERM`, `LANG`/`LC_*`, `OW_SANDBOX=1`, `OW_WORKSPACE`, plugin variables such as `ODOO_RC` and `MISE_*`). Known harnesses are auto-detected from `COMMAND` to mount only that harness's own state/config/binary paths and API env vars; override with `--profile <name|auto|none>` or `sandbox.profile` (e.g. `ow sandbox --profile claude` to open a shell prepared for `claude`).
- **Runtimes & local services**: plugins contribute extra read-only mounts and variables through `ow_sandbox_mounts` (shared virtualenvs and mise installs/config), the host's PostgreSQL socket directory `/var/run/postgresql` is always mounted read-only when it exists (a host Postgres, i.e. `postgres.enabled = false`, is reachable only through it), the network is shared (`--share-net`) so local services and LLM APIs work natively, and `ow_before_run` starts the shared services before entering the sandbox (Docker/Podman sockets are never mounted).
- **Extensibility & auditing**: pass `--ro PATH`, `--rw PATH`, `--env VAR` (repeatable) or set `sandbox.ro_paths`, `sandbox.rw_paths`, `sandbox.pass_env` via `ow config`. `-n/--dry-run` prints the exact `bwrap` command and forwarded env keys (secret values redacted) without executing.

---

## Repos

### `ow repo add NAME [URL] [--base REF] [-w WS]`
Register a bare repo under `$XDG_DATA_HOME/ow/repos/<name>.git` and, inside a workspace, add its worktree. Repos a plugin knows (`community`, `enterprise`, …) need no URL.

### `ow repo ls [-w WS]`
List registered bare repos and their `origin` URLs, marking the ones this workspace uses.

### `ow repo rm NAME [-n] [-y] [-w WS]`
Remove a repo's worktree from the workspace (refuses dirty worktrees or orphan commits; keeps unpushed local branches).

---

## Status, files & configuration

### `ow status [SPEC] [-w WS] [--only REPO…] [--fetch] [--offline]`
Show each repo's branch, base, upstream, ahead/behind counts, dirty state, and the plugins' status lines (GitHub PR state + Runbot build state, shared services, loaded in-repo plugins; TTL-cached; `--offline` skips the network).

- At a multi-stack root (or with `-w <multi-ws>`), inspects every stack. `--only` is refused there: pass `-w <stack>`.
- `--fetch`: runs parallel surgical fetches of tracked refs before printing status.
- `SPEC`: asks the status plugins about an external spec directly (e.g. `ow status enterprise#12345` or `ow status https://runbot.odoo.com/runbot/bundle/12345`). Mutually exclusive with `-w`, `--only` and `--fetch`.

### `ow refresh [-w WS]`
Recompute and write owned generated files (and sync `<workspace>.code-workspace` for multi-stack workspaces).

### `ow files [--diff] [-w WS]`
Show generated files and their ownership state (`absent`, `up to date`, `outdated`, `yours`, `skipped`), or unified diffs with `--diff`. Works at mono-stack workspaces, individual stacks, and multi-stack workspace roots (inspecting `<workspace>.code-workspace`). See [Generated files](files.md).

### `ow trust [PATH] [--untrust] [--list] [-w WS]`
Trust, untrust, or list the **locations** in-repo plugins may run from (`[tool.ow] plugin = "..."` in `pyproject.toml`, or `ow_plugin.py` at a repo root). Trust is by location, not content: a trusted plugin file or directory runs whatever code sits there, including code that arrives later, such as a PR checked out with `ow add`. See [Plugins](plugins.md#trust).

- `ow trust PATH`: trusts a plugin file or a directory (everything beneath it).
- `ow trust` with no `PATH`, on a terminal: asks whether to trust the plugin files found in the workspace, the workspace root, or its parent directory. Without a terminal it trusts the plugin files found. Then it refreshes the workspace so they take effect.
- `--untrust`: revokes `PATH`, or the workspace's plugin files; warns when a trusted directory still covers it.
- `--list`: prints every trusted file and directory (directories end in `/`).

### `ow config [--global] [-w WS]`
### `ow config get KEY [--global] [-w WS]`
### `ow config set KEY VALUE… [--global] [-w WS]`
### `ow config unset KEY [--global] [-w WS]`
Read and write typed plugin settings across the default, global, workspace and stack layers. At a multi-stack root, `set`/`unset` writes shared workspace-level settings; inside a stack or with `-w <stack>`, it writes stack-level overrides. Layers, file format and every setting: [Configuration](configuration.md).

---

## Fleet management

### `ow ls`
List indexed workspaces, their paths, and their repos (mono-stack) or stacks (multi-stack).

### `ow cd [WS]`
Print a workspace or stack path (`my-ws`, `review`, `review/iap`, or local `iap` inside `review/`). Used by `ow shell-init`.

### `ow shell-init SHELL`
Print the shell wrapper for `ow cd` and the completion script; `SHELL` is `bash`, `zsh` or `fish`.

### `ow rm WS [-n] [-y]`
Remove a mono-stack workspace, a whole multi-stack workspace (`ow rm review`), or a single stack (`ow rm review/iap` or `ow rm iap` inside `review/`), along with its worktrees and pushed branches. Refuses dirty worktrees, orphan commits, stray checkouts, and a workspace with a running detached process (`ow stop` first); in a multi-stack workspace every stack is preflighted before anything is deleted. While stacks remain, `<workspace>.code-workspace` is refreshed. Removing the last stack removes the multi-stack workspace: its recorded root files that still match the lock are deleted, hand-edited files stay. This is the only case where ow deletes generated files; `ow prune` follows the same rule.

### `ow prune [--offline] [-n] [-y]`
Clean up prunable worktree registrations, pushed orphan branches, dead index entries, and (unless `--offline`) clean workspaces or stacks whose checked-out branches all belong to `MERGED` or `CLOSED` GitHub PRs. A candidate with a running process, a dirty worktree or orphan commits is skipped with the reason.

---

## Retargeting

### `ow retarget [BASE] [--base REF] [--autostash] [--no-fetch] [--only REPO…] [-n] [-y] [-w WS]`
Guarded multi-repo replay (`cherry-pick --ff`, `last_taken`, `--fork-point` fallback).

- `BASE` and `--base` are mutually exclusive.
- Updates `[repos]` in `.ow/config.toml` as well as `branch.<b>.ow-base`, switches clean detached repos directly onto the new base via `git switch --detach`, and skips repos whose remote does not have the target series (e.g. `upgrade` when retargeting to `19.0`) with a clear note.
- Refreshes generated files (`mise`, shared venv, `odoorc`, editor configs) after mutation.
- Works on a stack from a multi-stack root via `-w <stack>`.

---

## Confirmation rules

Prompts (`Proceed?` after the plan is printed, `Drop it?` for `--reset`):

- `init`/`add` promotion of a mono-stack workspace to multi-stack, `repo rm`, `rm`, `prune`, and a `retarget` that is not a pure fast-forward.
- `run --reset`, which also shows what it will drop first.

Pass `-y` to confirm non-interactively or `-n` for a dry run. Without a terminal and without `-y` a prompting command exits 2 (`no terminal to confirm; re-run with -y`), as does answering no.

Git operations that fetch let SSH and credential prompts through on a terminal; without one they never prompt (`GIT_TERMINAL_PROMPT=0`, `ssh -o BatchMode=yes` unless you configured SSH yourself).
