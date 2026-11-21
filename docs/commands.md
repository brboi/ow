# Commands

Every ow command, its specs, flags, `--json` contract, and exit codes.

## Exit codes

- **0** — the command succeeded, or nothing needed doing.
- **1** — a repo was refused, a step failed, or a plugin diagnostic blocked refresh.
- **2** — a usage error, a declined prompt, or a prompt with no terminal and no `-y`.
- **130** — interrupted.

## Shared options

- **`-w, --workspace WS`** — workspace or stack to act on, by name or path (default: current directory). Inside a multi-stack workspace root, a bare stack name resolves to `./<stack>` first; from anywhere, use `<workspace>/<stack>` (e.g. `review/iap`).
- **`--only REPO`** — repeatable; limits `status` (single workspace/stack) and `retarget` to named repos.
- **`-n, --dry-run`** — print the plan without running it (`init`, `add`, `sandbox`, `repo rm`, `rm`, `prune`, `retarget`).
- **`-y, --yes`** — skip confirmation prompts (`init`/`add` promotion, `repo rm`, `rm`, `prune`, `retarget`).
- **`--json`** — emit canonical `schema_version: 1` JSON on `stdout` and route all progress/plans to `stderr` (`init`, `add`, `status`, `ls`, `rm`, `prune`, `files`, `config`, `retarget`). Implies non-interactive mode.
- **`--offline`** — skip external network lookups (`status`, `prune`).

---

## Specs

`ow init`, `ow add` (`SPEC`) and `ow run --tmp` (`TARGET`) share one grammar:

```
[<name>=]<item>[,<item>...]
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

When `<name>=` is omitted in a multi-stack context, `ow` derives the stack name from the spec (PR branch name, repo name, or base name) and deduplicates collisions (`-2`, `-3`).

---

## Workspace initialization, evolution & execution

### `ow init [SPEC…] [--branch BRANCH] [--as NAME] [-n] [-y] [--json]`

Initialize, compose, adopt, or repair the current directory (`.`) in place.

- 0 specs in an empty directory: creates an empty `.ow/config.toml` and registers `.` in the workspace index.
- `--branch BRANCH`: creates or checks out `BRANCH` in every added repo.
- 1 unnamed spec (e.g. `ow init enterprise#12345`, `ow init @18.0 --branch 18.0-fix`): initializes a mono-stack workspace directly in `.`.
- 1+ named specs or 2+ specs (e.g. `ow init odoo=community#56789,enterprise#12345 iap=iap#4321 voip=voip`): initializes a multi-stack workspace where each stack is a direct subdirectory marked `[workspace] stack = true`, with auto-allocated non-colliding `odoo.http_port` / `odoo.gevent_port` and a root `<workspace>.code-workspace`.
- Re-running `ow init` inside an existing or moved workspace repairs worktrees (`git worktree repair`), updates the workspace index, refreshes generated files (and `<workspace>.code-workspace`), and applies any additional `SPEC` arguments.
- Refuses to initialize a parent directory that already contains standalone (non-stack) ow workspaces.

### `ow add SPEC… [--branch BRANCH] [--as NAME] [-n] [-y] [--json]`

Evolve an already-initialized workspace (refuses outside a workspace with a hint to use `ow init` or `ow run --tmp`).

- Inside a mono-stack workspace (or a stack):
  - Unnamed specs add missing repos or attach a PR / feature branch onto an existing clean/detached worktree (refusing dirty worktrees, orphan commits, or unpushed work).
  - Named specs (`<name>=<spec>`) inside a mono-stack workspace promote it to multi-stack after confirmation (pass `-y` to skip prompt, `-n` to preview): moves existing worktrees and `.ow/` into `<existing-name>/` via staging, repairs git worktrees, preserves `odoo.db_name` and ports, creates the new stack(s), and writes `<workspace>.code-workspace`. Use `--as NAME` to pick the existing stack's name explicitly.
- At a multi-stack workspace root:
  - `<existing-stack>=<spec>` adds repos or attaches PRs into that existing stack.
  - `<new-stack>=<spec>` or an unnamed `<spec>` creates a new stack with coordinated ports and updates `<workspace>.code-workspace`.

### `ow run [TARGET…] [-w WS] [--tmp] [--fail-fast] [--reset] [-- ARGS…]`

Run one or more stacks locally via their plugin runner (`odoo` launches `odoo-bin -c odoorc`, preferring `$XDG_DATA_HOME/ow/venvs/py<ver>-<series>`, falling back to `mise exec` then `python3`).

- Single stack / mono-stack workspace: inherits the terminal TTY directly so interactive tools (`pdb`, `ow run -- shell`) work natively.
- Multi-stack root (`ow run` with no target, or multiple targets): runs all runnable stacks in parallel with `[stack]`-prefixed stdout/stderr (skipping non-runnable stacks such as `voip` with a note) and terminates process groups cleanly on `Ctrl+C`. Prints a `# summary:` line if any stack exits non-zero; pass `--fail-fast` to stop remaining stacks as soon as one exits non-zero.
- `--reset`: before starting each launched stack, drops its PostgreSQL database (`DROP DATABASE IF EXISTS ... WITH (FORCE)` via the managed `postgres` container, or `psql`/`dropdb` on a custom host) and removes its local filestore (`.odoo/filestore/<db>`), without prompting.
- Per-stack arguments: pass `TARGET:"ARGS"` (parsed with `shlex.split`, splitting on the first `:` outside `://` and `git@host:`), e.g.:
  ```sh
  ow run odoo:"-i voip --dev=all" iap:"--log-level=debug"
  ```
  Shared `-- ARGS…` are appended to every launched stack.
- No silent creation: without `--tmp`, `TARGET` resolves only existing workspaces/stacks.
- Ephemeral runs (`--tmp` / `--ephemeral`): creates temporary mono-stack or multi-stack checkouts in a temp directory with non-colliding ports, runs them, and cleans up worktrees and temp directories on exit:
  ```sh
  ow run --tmp enterprise#12345 -- -i sale
  ow run --tmp odoo=enterprise#12345:"-i voip" iap=iap#4321
  ```

### `ow exec [WS] [-w WS] [-- COMMAND [ARGS…]]`

Run a command (e.g. `pip install phonenumbers`, `pytest`, `odoo-bin shell`) or an interactive shell (when `COMMAND` is omitted) inside a workspace or stack's resolved Python virtualenv and plugin environment.

- **Strict `--` separator**: anything before `--` is an optional workspace or stack target (`WS`, mutually exclusive with `-w/--workspace`), and anything after `--` is executed as `COMMAND [ARGS…]`.
  ```sh
  ow exec -- pip install phonenumbers        # current workspace/stack
  ow exec iap -- pip install phonenumbers    # target a stack from a multi-stack root
  ow exec iap                                # open an interactive shell in stack `iap`
  ```
- **Multi-stack root**: without `WS`/`-w`, auto-selects the single stack with a Python environment (e.g. `odoo/` alongside `voip/`); refuses when multiple Python stacks exist and lists them.
- **Environment & `cwd`**: sets `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `OW_WORKSPACE`, clears `PYTHONHOME`, prepends `<venv>/bin` and plugin `PATH` entries (such as `<ws>/community` for `odoo-bin`), and injects plugin environment variables (such as `ODOO_RC`). Preserves the current directory when already inside the target stack, otherwise changes to its root.
- **Bootstrap & safety**: shares `ow run`'s virtualenv preparation (creating the venv via `mise` and installing requirements under a sidecar lock when needed, routing bootstrap output to `stderr` so `stdout` stays clean for pipes). Never falls back to system Python (refuses stacks without a Python environment or a missing virtualenv) and never starts container services (`postgres`/`mailsink`). In interactive shell mode (no `COMMAND`), prints `# <ws>: venv <path> (shared|local)` to `stderr`.

### `ow sandbox [-w WS] [--profile NAME] [--ro PATH…] [--rw PATH…] [--env VAR…] [-n] [-- COMMAND [ARGS…]]`

Run an arbitrary command, AI agent harness (`claude`, `opencode`, `codex`, `pi`, `omp`), or interactive shell (when `COMMAND` is omitted) inside a Linux bubblewrap (`bwrap`) sandbox scoped to a workspace, stack, or multi-stack root.

- **Filesystem isolation**: `$HOME` is an empty `tmpfs` (allowlist-only), system directories (`/usr`, `/etc`, `/opt`, …) are read-only, and only the target workspace root (or multi-stack root) is mounted read-write. Each stack's `.ow/` directory and each repo's `.git` pointer file are overlaid read-only so the sandboxed process cannot alter workspace configuration or replace git pointers.
- **Shared bare repos (partial RW)**: only bare repos referenced by the target workspace/stack(s) are mounted; each bare root, `config`, `hooks/`, and sibling workspaces' `worktrees/*` stay read-only while `objects/`, `refs/`, `logs/`, and this workspace's `worktrees/<id>/` are read-write — enabling `git add`/`commit`/`stash`/`rebase` without exposing hook/config sandbox-escape vectors. `~/.gitconfig` (and `~/.config/git/{config,ignore,attributes}`, never `credentials`) is mounted read-only.
- **Environment & harness profiles**: `bwrap --clearenv` starts from a strict allowlist (`HOME`, `USER`, `PATH`, `TERM`, `LANG`/`LC_*`, `OW_SANDBOX=1`, `OW_WORKSPACE`, plugin vars such as `ODOO_RC` and `MISE_*`). Known harnesses (`claude`, `opencode`, `codex`, `pi`, `omp`) are auto-detected from `COMMAND` to mount only that harness's own state/config/binary paths and API env vars; override with `--profile <name|auto|none>` or `sandbox.profile` (e.g. `ow sandbox --profile claude` to open a shell prepared for `claude`).
- **Runtimes & local services**: shared Python virtualenvs (`$XDG_DATA_HOME/ow/venvs/...`) and `mise` installs/config are contributed read-only by plugins (`ow_sandbox_mounts`), network is shared (`--share-net`) so local services and LLM APIs work natively, and `ow_before_run` starts shared local services (`postgres`, `mailsink`) before entering the sandbox (Docker/Podman sockets are never mounted).
- **Extensibility & auditing**: pass `--ro PATH`, `--rw PATH`, `--env VAR` (repeatable) or set `sandbox.ro_paths`, `sandbox.rw_paths`, `sandbox.pass_env` via `ow config`. Pass `-n`/`--dry-run` to inspect the exact `bwrap` command and forwarded env keys (with secret values redacted) without executing.

---

## Repos

### `ow repo add NAME [URL] [--base REF] [-w WS]`
Register a bare repo under `$XDG_DATA_HOME/ow/repos/<name>.git` and, inside a workspace, add its worktree. Repos the `odoo` plugin knows (`community`, `enterprise`, …) need no URL.

### `ow repo ls [-w WS]`
List registered bare repos and their `origin` URLs, marking the ones this workspace uses.

### `ow repo rm NAME [-n] [-y] [-w WS]`
Remove a repo's worktree from the workspace (refuses dirty worktrees or orphan commits; keeps unpushed local branches).

---

## Status, files & configuration

### `ow status [TARGET] [-w WS] [--only REPO…] [--fetch] [--offline] [--json]`
Show each repo's branch, base, upstream, ahead/behind counts, dirty state, and external plugin status (GitHub PR state + Runbot build state, TTL-cached; pass `--offline` to skip network).

- At a multi-stack root (or with `-w <multi-ws>`), inspects every stack and includes sibling stack paths, ports, and `.code-workspace` in `--json`.
- `--fetch`: runs parallel surgical fetches of tracked refs before printing status.
- `TARGET`: queries external status plugins directly (e.g. `ow status enterprise#12345` or `ow status https://runbot.odoo.com/runbot/bundle/12345`). Mutually exclusive with `-w`, `--only`, and `--fetch`.

### `ow refresh [-w WS]`
Recompute and write owned generated files (and sync `<workspace>.code-workspace` for multi-stack workspaces).

### `ow files [--diff] [--json] [-w WS]`
Show generated files and their ownership state (`absent`, `up to date`, `outdated`, `yours`, `skipped`), or unified diffs with `--diff`. Works at mono-stack workspaces, individual stacks, and multi-stack workspace roots (inspecting `<workspace>.code-workspace`).

### `ow config [--global] [--json] [-w WS]`
### `ow config get KEY [--global] [--json] [-w WS]`
### `ow config set KEY VALUE… [--global] [-w WS]`
### `ow config unset KEY [--global] [-w WS]`
Read and write typed plugin settings across default, global (`$XDG_CONFIG_HOME/ow/config.toml`), and workspace (`.ow/config.toml`) layers.

---

## Fleet management

### `ow ls [--json]`
List indexed workspaces, their paths, and their repos (mono-stack) or stacks (multi-stack).

### `ow cd [WS]`
Print a workspace or stack path (`my-ws`, `review`, `review/iap`, or local `iap` inside `review/`). Used by `ow shell-init`.

### `ow shell-init {bash|zsh|fish}`
Print the shell wrapper for `ow cd` and completion script.

### `ow rm WS [-n] [-y] [--json]`
Remove a mono-stack workspace, a whole multi-stack workspace (`ow rm review`), or a single stack (`ow rm review/iap` or `ow rm iap` inside `review/`), along with its worktrees and pushed branches. Refuses dirty worktrees, orphan commits, or stray checkouts (preflighting every stack before deleting anything in a multi-stack workspace). While stacks remain, `<workspace>.code-workspace` is refreshed. Removing the last stack removes the multi-stack workspace: its recorded root files that still match the lock are deleted, hand-edited files stay. This is the only case where ow deletes generated files; `ow prune` follows the same rule.

### `ow prune [--offline] [-n] [-y] [--json]`
Clean up prunable worktree registrations, pushed orphan branches, dead index entries, and (unless `--offline`) clean workspaces or stacks whose checked-out branches all belong to `MERGED` or `CLOSED` GitHub PRs.

---

## Retargeting

### `ow retarget [BASE] [--base REF] [--autostash] [--no-fetch] [--only REPO…] [-n] [-y] [--json] [-w WS]`
Guarded multi-repo replay (`cherry-pick --ff`, `last_taken`, `--fork-point` fallback).

- `BASE` and `--base` are mutually exclusive.
- Updates `[repos]` in `.ow/config.toml` as well as `branch.<b>.ow-base`, switches clean detached repos directly onto the new base via `git switch --detach`, and skips repos whose remote does not have the target series (e.g. `upgrade` when retargeting to `19.0`) with a clear note.
- Refreshes generated files (`mise`, shared venv, `odoorc`, editor configs) after mutation.
- Works on a stack from a multi-stack root via `-w <stack>`.

---

## Confirmation rules & `--json` contract

- Prompts: mutating/destructive commands prompt (`init`/`add` promotion to multi-stack, `repo rm`, `rm`, `prune`, and non-fast-forward `retarget`). Pass `-y` to confirm non-interactively or `-n` for a dry run.
- `--json` envelope: supported on `init`, `add`, `status`, `ls`, `rm`, `prune`, `files`, `config`, `config get`, and `retarget`. Every JSON response is a UTF-8 JSON object on `stdout` carrying `"schema_version": 1`; all progress lines, plans, and warnings go to `stderr`, and interactive prompts are disabled (pass `-y` for prompting commands).
