# Commands

This page is the reference for every `ow` command: synopsis, flags, exit codes and confirmation rules. `ow <command> --help` shows the same arguments, with the options folded into `[OPTIONS]`.

- [Naming](#naming), [Exit codes](#exit-codes), [Shared options](#shared-options), [Specs](#specs), [Bases and upstreams](#bases-and-upstreams)
- Workspaces: [`init`](#ow-init), [`add`](#ow-add), [`run`](#ow-run), [`stop`](#ow-stop), [`logs`](#ow-logs), [`exec`](#ow-exec), [`sandbox`](#ow-sandbox)
- Git: [`rebase`](#ow-rebase), [`status`](#ow-status), [`repo`](#repos)
- Files and settings: [`refresh`](#ow-refresh), [`files`](#ow-files), [`trust`](#ow-trust), [`config`](#ow-config)
- Fleet: [`ls`](#ow-ls), [`cd`](#ow-cd), [`shell-init`](#ow-shell-init), [`rm`](#ow-rm), [`prune`](#ow-prune)
- [Confirmation rules](#confirmation-rules)

## Naming

- **`WS`** is a workspace, by name or path. Where a stack is accepted it is `WS[/STACK]`: `review` is a workspace and `review/iap` is one of its stacks. Resolution order: a path (`./iap` inside a multi-stack root wins), `<workspace>/<stack>`, an indexed workspace name, then a stack name that is unique across indexed multi-stack workspaces. An ambiguous name is a usage error that lists the matches.
- **`STACK`** is the name of a stack inside a multi-stack workspace (`--as STACK`, `[stacks.<stack>]`).
- **`SPEC`** says what to check out. See [Specs](#specs): `enterprise#12345`, `@18.0`, a Runbot URL, `iap=iap#4321`.

## Exit codes

- **0**: the command succeeded, or nothing needed doing.
- **1**: a repo was refused, a step failed, a plugin hook failed, or a plugin diagnostic blocked a refresh. `ow logs` with no log to show also exits 1.
- **2**: a usage error, a declined prompt, or a prompt with no terminal and no `-y`.
- **130**: interrupted with Ctrl+C.
- **The child's code.** `ow run`, `ow exec` and `ow sandbox` pass on the exit code of the process they ran. So does `ow stop` when a plugin's `ow_stop` supplies the command. Death by signal `n` is `128 + n`. A `1` or `2` from these commands may be the child's, not ow's. At a multi-stack root, `ow run` returns the code of the first stack, in stack order, that exited non-zero.

## Shared options

- **`-w, --workspace WS`**: the workspace or stack to act on. The default is the current directory's. It excludes a positional `WS`, `SPEC` or `PATH`.
- **`--only REPO`**: repeatable. Limits `status` (one workspace or stack) and `rebase` to the named repos.
- **`-n, --dry-run`**: print the plan without running it. Accepted by `init`, `add`, `sandbox`, `repo rm`, `rm`, `prune` and `rebase`.
- **`-y, --yes`**: skip confirmation prompts. Accepted by `init`, `add`, `repo rm`, `rm`, `prune`, `rebase`, `trust` and `run --reset`.
- **`--offline`**: skip external network lookups (`status`, `prune`).

---

## Specs

`ow init`, `ow add` and `ow run --tmp` share one grammar:

```
[<stack>=]<item>[,<item>...]
```

Items within a stack are joined with a comma. An `<item>` takes one of these forms:

1. **GitHub PR** (`github` plugin): `enterprise#12345`, `odoo/iap-apps#42`, `enterprise/pull/12345` or a PR URL.
   - It fetches `refs/pull/<num>/head`.
   - It adds the open sister PRs on the same branch across the known repos of the same org.
   - It adds the Odoo core next to an addon repo, detached on the base series.
   - It pins detached companion repos to Runbot's exact `base_match` SHA when available and prints the SHA, or a fallback note when Runbot is unreachable.
   - The branch gets the PR's head branch as its [upstream](#bases-and-upstreams).
2. **Runbot bundle or batch URL** (`runbot` plugin): `https://runbot.odoo.com/runbot/bundle/<id_or_name>` or `.../batch/<id>`. It checks out the exact batch commits, detached. It keeps only the repos that are registered locally or listed in `odoo.default_repos`, so ow never clones an unregistered repo on its own.
3. **`<repo>@<ref>`** or `<repo>@<remote>/<ref>`: resolves `<ref>` across the configured remotes. `origin` wins a tie, then local branches.
   - A base or series ref (`master`, `main`, `18.0`, `saas-*`, `origin/HEAD`) is checked out detached, so workspaces never collide on a local branch.
   - Any other ref is a feature branch. It is checked out as a local branch, and its base is inferred (the Odoo series for an Odoo repo).
   - When the inference falls back to `origin/HEAD`, ow warns and suggests the next form. A branch that no remote has is an error that suggests it too.
4. **`<repo>@<base>..<branch>`** or `<repo>@<base>..<remote>/<branch>`: names the base. ow checks out `<branch>` when a remote has it and creates it from `<base>` otherwise. `<remote>/` says which remote holds it. The branch records `<base>` as its base. Example: `enterprise@18.0..18.0-my-fix`.
5. **`@<base>`** or `@<base>..<branch>`: the same on every repo of `odoo.default_repos` (default `["community", "enterprise"]`). `@18.0` leaves them detached on `18.0`. `@18.0..18.0-my-fix` puts them on a new branch `18.0-my-fix` based on `origin/18.0`.
6. **`<repo>`**: a registered or known repo (`voip`, `upgrade`), detached on the stack's shared base when `origin` has it, else on `origin/HEAD`.

When `<stack>=` is omitted in a multi-stack context, ow derives the stack name from the spec (PR branch name, repo name or base name) and numbers collisions (`-2`, `-3`). Plugins resolve PRs and URLs through `ow_resolve_spec` ([Plugins](plugins.md#hook-reference)). A repo named twice in one stack is refused.

## Bases and upstreams

- **Base.** Each branch remembers the remote branch it is kept level with, for example `origin/18.0`, as `branch.<b>.ow-base` in the bare repo. The workspace's `[repos]` table holds the default base of each repo, used when ow creates a branch there or leaves it detached. A branch's own base wins.
- **Upstream of a PR branch.** The PR's head branch. For a PR from the same repo it is `origin/<head>`. For a fork, ow adds a remote named after the fork owner's login to the bare (ssh or https, like `origin`), fetches it and tracks `<login>/<head>`. When ow cannot find the head branch, it warns and sets no upstream.
- **A branch that was never pushed.** ow pre-sets `branch.<b>.remote=origin` and `branch.<b>.merge=refs/heads/<b>`. A bare `git pull` then fails cleanly, and the first bare `git push` creates the branch.
- **Narrow fetch refspec.** A bare repo has no wide fetch refspec. For each tracked branch ow adds `+refs/heads/<b>:refs/remotes/<r>/<b>` on its remote, so `@{u}` resolves.
- **Not pushed yet.** `ow status` shows an upstream whose remote ref is missing as `<remote>/<b> (not pushed)`. `ow status --fetch` and `ow rebase` check quietly whether it has been pushed since.

---

## Workspace initialization, evolution and execution

### `ow init`

```
ow init [SPEC…] [--as STACK] [-n] [-y]
```

Initialize, compose, adopt or repair the current directory (`.`) in place.

- With no spec in an empty directory, it creates an empty `.ow/config.toml` and registers `.` in the workspace index.
- With one unnamed spec (`ow init enterprise#12345`, `ow init @18.0..18.0-fix`), it initializes a mono-stack workspace in `.`.
- With named specs or two or more specs (`ow init odoo=community#56789,enterprise#12345 iap=iap#4321 voip=voip`), it initializes a multi-stack workspace. There is one root `.ow/config.toml` with a `[stacks.<stack>]` table per stack. Each Odoo stack gets its own `odoo.http_port` and `odoo.gevent_port`. The root gets `<workspace>.code-workspace`.
- A new workspace whose `unique` setting (`odoo.db_name`) is still at its default and equals another indexed workspace's gets `<value>-2` in its config. The next free suffix is used.
- Run in an existing workspace, it repairs worktrees, updates the index, refreshes the generated files and applies the specs you add.
- Run in a workspace written by 3.0.0a1 or 2.x, it migrates `.ow/config.toml`. See [Migrating](migrating-to-3.0.md#from-300a1-and-2x).
- It refuses a parent directory that already contains standalone (non-stack) workspaces.

**Plan.** `ow init` and `ow add` gather what exists, then print the whole plan before they act. A header says `# create workspace <name> at <path>` or `# update workspace …` (`stack` for a stack). Cloning a repo is a step of the plan. `-n` stops after the plan. A refusal changes nothing. When a unit fails or you press Ctrl+C, ow undoes it: a new workspace is removed, and an existing one keeps the repos it already added.

**Moved workspace.** After `mv`, ow repairs the moved worktrees with `git worktree repair`. A worktree that `git worktree prune` already forgot is refused (exit 2) and its files stay untouched. ow removes a worktree only when it is provably empty, locked as `initializing`, or clean according to `git status --porcelain --ignored`. It never removes a non-empty directory.

### `ow add`

```
ow add SPEC… [--as STACK] [-n] [-y]
```

Evolve an initialized workspace. Outside a workspace it refuses and points at `ow init` and `ow run --tmp`. It prints the same plan as `ow init`. `ow add a b` merges the specs.

- In a mono-stack workspace or a stack, an unnamed spec adds missing repos or attaches a PR or feature branch to an existing clean or detached worktree. It refuses dirty worktrees, orphan commits and unpushed work.
- In a mono-stack workspace, a named spec (`<stack>=<spec>`) promotes it to multi-stack after confirmation. `-y` skips the prompt and `-n` previews. ow moves the existing worktrees and `.ow/` into `<existing-name>/` through a staging directory, repairs the git worktrees, keeps `odoo.db_name` and the ports, creates the new stacks and writes `<workspace>.code-workspace`. `--as STACK` names the existing stack.
- At a multi-stack root, `<existing-stack>=<spec>` adds to that stack. `<new-stack>=<spec>` or an unnamed spec creates a stack with its own ports and updates `<workspace>.code-workspace`.

### `ow run`

```
ow run [WS…] [-w WS] [-d] [--tmp] [--fail-fast] [--reset] [-y] [-- ARGS…]
```

Run one or more stacks with the runner a plugin provides. The `odoo` plugin runs `odoo-bin -c odoorc`. It prefers the workspace virtualenv, then `mise exec`, then `python3`. Before it starts, `ow_before_run` starts the [shared services](configuration.md#shared-services) and refreshes the generated files that depend on them.

- **Single stack or mono-stack workspace.** The runner inherits the terminal, so `pdb` and `ow run -- shell` work.
- **Multi-stack root** (no `WS`, or several). All runnable stacks run in parallel with `[stack]` prefixes on their output. A stack with no runner, such as `voip`, is skipped with a note. Ctrl+C ends the process groups cleanly. If any stack exits non-zero, ow prints a `# summary:` line. `--fail-fast` stops the others as soon as one fails.
- **`-d, --detach`.** Start in the background. ow records the PID under `<root>/.ow/run/` and the output under `<root>/.ow/logs/`. A runner that detaches itself is started and left alone. Each detached start truncates the stack's log. It refuses when the stack already runs and points at `ow stop` and `ow logs -f`. It cannot combine with `--tmp`.
- **Runner that detaches itself.** In the foreground, ow follows its logs like `ow logs -f` and stops it when nothing runs any more or when you press Ctrl+C (exit 130). With `-d`, ow starts it and stops nothing. The contract is in [Plugins](plugins.md#run-stop-and-logs-one-hook-per-subject).
- **`--reset`.** Before each launched stack starts, ow shows what it will drop and asks `Drop it?`.
  - The list holds `database <user>@<host>:<port>/<db>`, the local filestore `.odoo/filestore/<db>`, and `<plugin>: local state` for a plugin that resets something without describing it.
  - `-y` skips the question. Without a terminal and without `-y`, or after you answer no, it is a usage error (exit 2) and nothing is dropped.
  - The drop uses the managed `postgres` container (`DROP DATABASE IF EXISTS ... WITH (FORCE)`), or `psql` and `dropdb` for a custom host. ow refuses the system databases `postgres`, `template0` and `template1`.
- **Per-stack arguments.** Pass `WS:"ARGS"`. ow splits it with `shlex.split` on the first `:` outside `://` and `git@host:`:
  ```sh
  ow run odoo:"-i voip --dev=all" iap:"--log-level=debug"
  ```
  `-- ARGS…` goes to the one launched stack. It cannot combine with several `WS`. At a multi-stack root, `ow run -- ARGS` is refused when several stacks are runnable: name the stack.
- **No silent creation.** Without `--tmp`, `WS` names an existing workspace or stack. A token that names none is one error: `unknown workspace or stack X; did you mean `ow run --tmp X`…`.
- **`--tmp`, `--ephemeral`.** Needs at least one `SPEC`. ow prints its plan, creates a temporary mono-stack or multi-stack checkout with its own ports, runs it and removes the worktrees and the temporary directory on exit:
  ```sh
  ow run --tmp enterprise#12345 -- -i sale
  ow run --tmp odoo=enterprise#12345:"-i voip" iap=iap#4321
  ```

### `ow stop`

```
ow stop [WS…] [-w WS] [--services] [--all]
```

Stop detached workspaces or stacks, and the shared service containers.

- Without `--services` or `--all`, it stops only the named workspaces or stacks. A detached process group gets SIGTERM, then SIGKILL. A plugin's `ow_stop` hook stops what the plugin started. The shared containers keep running.
- `--services` stops the shared service containers (`ow-postgres`, `ow-pgweb`, `ow-mailsink`). Their data survives.
- `--all` stops every running indexed workspace and all shared services. It cannot combine with `WS` or `-w`.

### `ow logs`

```
ow logs [WS…] [-w WS] [-f] [-n LINES]
```

Print the last `LINES` lines (default 50) of the logs of a detached workspace or stack. The logs are `<root>/.ow/logs/<stack>.log`, or the files a plugin's `ow_logs` hook names. Several logs are interleaved chronologically, each line prefixed with `[stack]`.

- `-f, --follow` keeps following new output until the process stops, or until you press Ctrl+C (exit 130). A last read picks up what it wrote on exit.
- With nothing to show, it exits 1 with `no logs found; start the workspace with ow run -d first`. The log of a stopped workspace still prints (exit 0).
- ow never rotates logs. The next `ow run -d` starts a fresh one.

### `ow exec`

```
ow exec [WS] [-w WS] [-- COMMAND [ARGS…]]
```

Run a command (`pip install phonenumbers`, `pytest`, `odoo-bin shell`) or, without `COMMAND`, an interactive shell, in the stack's virtualenv and plugin environment. The exit code is the command's.

- **`--` separates.** What comes before it is an optional `WS` (exclusive with `-w`). What comes after is the command.
  ```sh
  ow exec -- pip install phonenumbers        # current workspace or stack
  ow exec iap -- pip install phonenumbers    # a stack of a multi-stack root
  ow exec iap                                # interactive shell in stack `iap`
  ```
- **Multi-stack root.** Without `WS` or `-w`, ow picks the one stack that has a Python environment. With several it refuses and lists them.
- **Environment.** ow sets `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT` and `OW_WORKSPACE`, clears `PYTHONHOME`, prepends `<venv>/bin` and the plugins' `PATH` entries (such as `<ws>/community` for `odoo-bin`), and adds the plugins' variables (such as `ODOO_RC`). It keeps the current directory when you are inside the stack, else it changes to the stack root.
- **Python stacks.** ow prepares the virtualenv like `ow run` does, creating it through the environment plugin and installing the requirements under a sidecar lock. Bootstrap output goes to stderr, so stdout stays clean for pipes. If the configured virtualenv is missing, ow never falls back to the system Python.
- **Stacks without a Python environment.** The command runs in the stack directory, wrapped by the environment plugin when it offers a wrapper (`mise exec --`).
- **No services.** `ow exec` never starts the shared services.
- **Shell mode.** ow prints a line on stderr: `# <ws>: venv <path> (shared|local)`, or `managed environment`, or `shell`.

### `ow sandbox`

```
ow sandbox [WS] [-w WS] [-p PROFILE] [--ro PATH…] [--rw PATH…] [--env VAR…] [-n] [-- COMMAND [ARGS…]]
```

> **Experimental.** `ow sandbox` may change or break during 3.x, and it is outside the compatibility promise. It prints `# experimental: …` on stderr every time, a dry run included.

Run a command, an AI agent harness (`claude`, `opencode`, `codex`, `pi`, `omp`) or, without `--`, an interactive shell, inside a Linux bubblewrap (`bwrap`) sandbox scoped to a workspace, stack or multi-stack root. Arguments outside `--` are a usage error (exit 2), except `WS`. The exit code is the command's. `sandbox.enabled = false` refuses the command (exit 1).

**Threat model.** The sandbox protects the host's secrets during the session: `~/.ssh`, git credentials, and every environment variable it does not forward. It does not protect the host from what a session leaves behind in the workspace, in the shared bare repos or in a harness's config. See [Residual risks](#residual-risks).

**What the session sees**

- **System.** `/usr`, `/etc`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/opt`, `/nix` and the directory holding `resolv.conf` under `/run` are read-only. `/dev`, `/proc`, `/tmp` and `$HOME` are empty tmpfs. The network is shared, so local services and LLM APIs work.
- **In `$HOME`, read-only.** The git config files (never `credentials`), the auto-detected harness binary and the profile's `ro_paths`.
- **In `$HOME`, read-write.** The profile's own state directories and files. ow mounts only paths that exist. It creates nothing on the host except `<bare>/logs`. The state of a harness whose directory does not exist on the host is lost when the session ends.
- **Workspace root, read-write**, with read-only overlays on `<root>/.ow/`, each repo's `.git` file, every file of the lock that exists on disk, and the approved in-repo plugin files.
- **Shared bare repos.** Read-only, except `objects/`, `refs/`, `logs/` and this workspace's `worktrees/<id>/`. In that directory `commondir` and `gitdir` are read-only again. `hooks/`, `config` and the other workspaces' worktrees stay read-only. `git add`, `commit`, `stash` and `rebase` work.
- **Plugins.** `ow_sandbox_mounts` adds read-only and read-write paths and variables: the shared virtualenvs, mise's installs and config, and the Postgres socket directory (`postgres` plugin, `/run/postgresql` and `/var/run/postgresql` when they exist). `mise` also trusts the workspace's fragment inside the sandbox through `MISE_TRUSTED_CONFIG_PATHS`.
- **Extra mounts.** `sandbox.ro_paths`, `sandbox.rw_paths`, `--ro` and `--rw` apply last.
- **Environment.** `bwrap --clearenv`, then an allowlist: `HOME`, `USER`, `PATH`, `TERM`, `LANG` and `LC_*`, the git identity variables, `OW_SANDBOX=1`, `OW_WORKSPACE`, the plugins' variables (`ODOO_RC`, `MISE_*`), the profile's API variables, and what `--env` and `sandbox.pass_env` name.
- **Namespaces.** IPC, PID, UTS and cgroup are unshared. The session dies with ow.
- **Profiles.** Five profiles exist, one per harness. `--profile <name|auto|none>` or `sandbox.profile` picks one. `auto` detects it from `COMMAND`. `none` mounts nothing extra.
- **Services.** `ow_before_run` starts the shared services before the sandbox opens. Docker and Podman sockets are never mounted.

**`-n, --dry-run`** prints the exact `bwrap` command and the forwarded variables. A variable whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD` or `CREDENTIAL`, or is the whole word `AUTH` or `AUTHORIZATION`, shows `<redacted>`.

#### Residual risks

- **Harness configs are writable.** A session can plant instructions or hooks in `~/.claude`, `~/.codex` or `~/.config/opencode`. The harness reads them later, outside the sandbox.
- **Shared refs.** `refs/` and `logs/` are writable and shared by every workspace of the same bare repo. A session can rewrite the branches of another workspace. The reflog recovers them.
- **Plugin files.** Only the in-repo plugin file is protected, not the modules it might load next to it.
- **Git limits.** `packed-refs` and `config` are read-only, so `git branch -D` on a packed branch, `gc --auto` and `pack-refs` fail. Git cannot rewrite a protected file inside the session.
- **Running services.** The shared services start before the sandbox and stay reachable over the shared network.
- **Network.** The session reaches whatever the host reaches.

---

## Repos

### `ow repo add`

```
ow repo add NAME [URL] [--base REF] [-w WS]
```

Register a bare repo under `$XDG_DATA_HOME/ow/repos/<name>.git` and, inside a workspace, add its worktree. A repo a plugin knows (`community`, `enterprise`) needs no URL. When it finds a bare left by ow 2.x, it repairs it in place (`remote.origin.tagOpt --no-tags`, `core.logAllRefUpdates true` and `origin/HEAD`) without adding a wide fetch refspec.

### `ow repo ls`

```
ow repo ls [-w WS]
```

List the registered bare repos and their `origin` URLs. It marks the ones this workspace uses.

### `ow repo rm`

```
ow repo rm NAME [-n] [-y] [-w WS]
```

Remove a repo's worktree from the workspace. It refuses dirty worktrees and orphan commits. It keeps unpushed local branches.

---

## Git

### `ow rebase`

```
ow rebase [BASE] [--autostash] [--no-fetch] [--only REPO…] [-n] [-y] [-w WS]
```

Guarded multi-repo update. `ow rebase` has two uses.

**Update my branch on its base: `ow rebase`.** With no argument, ow fetches the tracked refs of each repo (`--no-fetch` skips it), then plans every repo and prints the plan:

- A branch is rebased onto its base.
- A detached repo advances to the new tip of its base.
- Commits a colleague pushed to the branch's upstream come in first. A branch with no local commit fast-forwards (`cherry-pick --ff`). A branch with local commits has them replayed on the upstream (`rebase --fork-point`), then on the base. An upstream equal to the base is never treated as a colleague's branch.
- A repo with no base is refused: `no base; run `ow init` to migrate an older workspace, or `ow rebase <base>` to set one`.
- A repo with modified files is refused unless you pass `--autostash`. So is a repo in the middle of a git operation.
- ow asks before it runs, except when every repo only fast-forwards.

**Change series: `ow rebase BASE`.** `BASE` is a series or ref (`19.0`, `origin/19.0`). ow moves every repo onto it: a branch is replayed with `rebase --onto`, and a clean detached repo switches with `git switch --detach`. A repo whose remote lacks that branch (`upgrade` when you move to `19.0`) is skipped with a note. ow records `BASE` as the base of each branch (`branch.<b>.ow-base`) and in `[repos]` of `.ow/config.toml`.

After either, ow refreshes the generated files when a repo changed. On a multi-stack workspace, pass `-w <stack>` to act on one stack.

### `ow status`

```
ow status [SPEC] [-w WS] [--only REPO…] [--fetch] [--offline]
```

Show each repo's branch, base, upstream, ahead and behind counts and dirty state, then the plugins' status lines: GitHub PR state, Runbot build state, shared services and loaded in-repo plugins. Network lookups are cached with a TTL. `--offline` skips the network.

- At a multi-stack root, or with `-w <multi-ws>`, it inspects every stack. `--only` is refused there: pass `-w <stack>`.
- `--fetch` runs parallel narrow fetches of the tracked refs first.
- `SPEC` asks the status plugins about an external spec (`ow status enterprise#12345`, `ow status https://runbot.odoo.com/runbot/bundle/12345`). It excludes `-w`, `--only` and `--fetch`.

---

## Files and settings

### `ow refresh`

```
ow refresh [-w WS]
```

Recompute and write the owned generated files. For a multi-stack workspace it also syncs `<workspace>.code-workspace`.

### `ow files`

```
ow files [--diff] [-w WS]
```

Show the generated files and their state (`absent`, `up to date`, `outdated`, `yours`, `skipped`), or unified diffs with `--diff`. It works on a mono-stack workspace, a stack and a multi-stack root. See [Generated files](files.md).

### `ow trust`

```
ow trust [PATH] [--untrust] [--list] [-w WS] [-y]
```

Trust, untrust or list the **locations** from which in-repo plugins may run. Trust is by location, not by content. A trusted plugin file or directory runs whatever code sits there, including code that arrives later, such as a PR checked out with `ow add`. See [Plugins](plugins.md#trust).

- `ow trust PATH` trusts a plugin file or a directory (everything beneath it). It asks nothing.
- `ow trust` with no `PATH`, on a terminal, asks whether to trust the plugin files found in the workspace, the workspace root or its parent directory. With `-y` it trusts the plugin files found without asking. Without a terminal and without `-y` it exits 2. After trusting, it refreshes the workspace so the plugins take effect.
- `--untrust` revokes `PATH`, or the workspace's plugin files, without asking. It warns when a trusted directory still covers the path.
- `--list` prints every trusted file and directory. Directories end in `/`.
- A corrupt trust file is an error. ow rewrites nothing.

### `ow config`

```
ow config [--global] [-w WS]
ow config get KEY [--global] [-w WS]
ow config set KEY VALUE… [--global] [-w WS]
ow config unset KEY [--global] [-w WS]
```

Read and write typed plugin settings across the default, global, workspace and stack layers. At a multi-stack root, `set` and `unset` write workspace-wide settings. Inside a stack, or with `-w <stack>`, they write stack overrides. Layers, file format and every setting are in [Configuration](configuration.md).

---

## Fleet management

### `ow ls`

List the indexed workspaces, their paths, and their repos (mono-stack) or stacks (multi-stack).

### `ow cd`

```
ow cd [WS]
```

Print a workspace or stack path: `my-ws`, `review`, `review/iap`, or `iap` inside `review/`. `ow shell-init` wraps it.

### `ow shell-init`

```
ow shell-init SHELL
```

Print the shell wrapper for `ow cd` and the completion script. `SHELL` is `bash`, `zsh` or `fish`.

### `ow rm`

```
ow rm WS [-n] [-y]
```

Remove a mono-stack workspace, a whole multi-stack workspace (`ow rm review`) or one stack (`ow rm review/iap`, or `ow rm iap` inside `review/`), with its worktrees and pushed branches.

- It refuses dirty worktrees, orphan commits, stray checkouts and a workspace with a running process. Run `ow stop` first.
- In a multi-stack workspace, every stack is checked before anything is deleted.
- While stacks remain, `<workspace>.code-workspace` is refreshed.
- Removing the last stack removes the multi-stack workspace. Its recorded root files that still match the lock are deleted. Hand-edited files stay. This is the only case where ow deletes generated files. `ow prune` follows the same rule.

### `ow prune`

```
ow prune [--offline] [-n] [-y]
```

Clean up prunable worktree registrations, pushed orphan branches and dead index entries. Unless `--offline`, it also removes clean workspaces or stacks whose checked-out branches all belong to `MERGED` or `CLOSED` GitHub PRs. A candidate with a running process, a dirty worktree or orphan commits is skipped with the reason.

---

## Confirmation rules

ow prompts (`Proceed?` after it prints the plan, `Drop it?` for `--reset`) for:

- the promotion of a mono-stack workspace to multi-stack by `init` or `add`;
- `repo rm`, `rm` and `prune`;
- an `ow rebase` that is not a pure fast-forward;
- `ow trust` without `PATH`, on a terminal;
- `run --reset`, which shows what it will drop first.

Pass `-y` to confirm non-interactively or `-n` for a dry run. Without a terminal and without `-y`, a prompting command exits 2 (`no terminal to confirm; re-run with -y`). Answering no also exits 2.

**SSH and credentials.** On a terminal, git's SSH and credential prompts pass through to you. When several repos fetch in parallel and one fails with an SSH key denial, ow retries it alone so the passphrase prompt stays readable. Without a terminal nothing prompts: ow sets `GIT_TERMINAL_PROMPT=0` and `ssh -o BatchMode=yes`, unless you set `GIT_SSH_COMMAND`, `GIT_SSH` or a global `core.sshCommand` yourself. For unattended use, load your key with `ssh-add` or configure a credential helper.
