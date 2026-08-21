# Migrating to ow 2.0

ow 1.x was project-scoped: one `ow.toml` at a project root, with `templates/`,
`services/`, `workspaces/` and `.bare-git-repos/` beside it. 2.0 is user-level.
There is no project root any more.

Nothing migrates itself. This is a one-time move you do by hand. It takes a few
minutes and it keeps your bare repos — no re-cloning.

Set this once, and paste the rest as is:

```sh
OLD=~/src/odoo          # your old project root, the directory holding ow.toml
```

Paths below are the XDG defaults. ow reads `XDG_CONFIG_HOME`, `XDG_DATA_HOME`
and `XDG_STATE_HOME`; if you set any of them, substitute accordingly.

## What breaks

| 1.x | 2.0 |
|---|---|
| `$OLD/ow.toml` | `~/.config/ow/config.toml`, one per user |
| `$OLD/.bare-git-repos/` | `~/.local/share/ow/repos/` |
| `$OLD/templates/` | packaged inside ow; per-file overrides in `~/.config/ow/templates/` |
| `$OLD/services/` | packaged inside ow; no ow command reads it |
| `$OLD/workspaces/<name>/` | anywhere; an index at `~/.local/state/ow/workspaces` remembers where |
| `.ow/config` | `.ow/config.toml` |
| `OW_WORKSPACE=<name>` or a path | an absolute path only |
| project root | gone |

Until you migrate, ow refuses to run and points back here. It detects two
things: an old `ow.toml` at or above the current directory while no global
config exists yet, and a `.ow/config` with no `.ow/config.toml` beside it.

## Migrate

### 1. Global config

Your old `ow.toml` holds `[vars]` and `[remotes]`. The new global config takes
exactly the same shape, so copy it verbatim.

```sh
mkdir -p ~/.config/ow
cp "$OLD/ow.toml" ~/.config/ow/config.toml
```

Carries over: every key under `[vars]`, and every entry under `[remotes]` with
its `url`, `pushurl` and `fetch`.

Does not carry over: nothing else was ever read from that file. What is gone is
the project root it used to define — the location of your bare repos, templates
and workspaces was derived from where `ow.toml` sat, and each of those is now
resolved on its own.

To start clean instead, skip the copy and run `ow init`, `ow apply`,
`ow status` or `ow rebase` **from outside `$OLD`**: ow writes a
commented default with the community remote and tells you where it put it.
Inside `$OLD` the check above fires first — ow sees the old `ow.toml` and
stops, which is the one thing that would leave you going in circles. `ow ls`,
`ow prune` and `ow templates` need no configuration, so they never create it.

You can leave the old `ow.toml` in place. Once `~/.config/ow/config.toml`
exists, ow stops looking for it.

### 2. Bare repos — move them, do not copy

```sh
mkdir -p ~/.local/share/ow
mv "$OLD/.bare-git-repos" ~/.local/share/ow/repos
```

`mv`, not `cp`. These are several gigabytes; a copy leaves you with two of them,
and only one keeps receiving fetches. The layout inside is unchanged — one
`<alias>.git` directory per repo.

### 3. Repair the worktrees

Every worktree's `.git` file holds an absolute path to its bare repo, so moving
the bare repos breaks all of them:

```
fatal: not a git repository: (null)
```

git fixes this itself. Run it once per bare repo:

```sh
for repo in ~/.local/share/ow/repos/*.git; do
    git -C "$repo" worktree repair
done
```

Do this before running any ow command. Skipping it does not fail cleanly:
`ow apply` reconciles each worktree's attached/detached state by running git
commands inside it, and the broken `.git` file makes those fail. You get a
Python traceback ending in `CalledProcessError: ... 'switch' ... returned
non-zero exit status 128`, with `fatal: not a git repository: (null)` printed
above it. `ow status` on the same workspace prints `community: (error)` and
moves on, no traceback. Neither is a bug report waiting to happen — the fix is
the `git worktree repair` above, run once per bare repo.

### 4. Rename each workspace config

```sh
for ws in "$OLD"/workspaces/*/; do
    [ -f "$ws/.ow/config" ] && mv "$ws/.ow/config" "$ws/.ow/config.toml"
done
```

The contents do not change: same `templates`, `repos` and `vars`.

Workspaces no longer have to sit under a `workspaces/` directory — they can
live anywhere. If you intend to move them, do it now, before the next step.

### 5. Let ow discover the workspaces

`ow ls`, and looking a workspace up by bare name, read a discovery index at
`~/.local/state/ow/workspaces`. It is a plain list of paths, not a database:
the `.ow/config.toml` on disk remains the only truth. The index self-heals —
every workspace ow successfully resolves is remembered, and entries that no
longer point at a workspace are dropped as the file is read.

Resolving a workspace registers it, so a single `ow apply` per workspace both
registers it and re-renders its files against the new global config:

```sh
for ws in "$OLD"/workspaces/*/; do
    ow apply "$ws"
done
ow ls
```

Until a workspace has been resolved once, `ow status <name>` cannot find it by
name. Pass a path (`ow status ./name`) or `cd` into it.

## Templates: take only what you changed

Templates now ship inside ow. Do not copy `$OLD/templates/` across wholesale —
that turns every packaged file into a frozen copy of your own, which stops
receiving ow's improvements.

`ow templates` lists every file ow ships and its state, but that state
describes your new setup, not `$OLD` — it has no way to tell you which files
you modified back then. Diff each one yourself: `--take` gets you a pristine
copy of the packaged version to diff against.

```sh
ow templates                              # every file, with its state
ow templates --take common/odoorc.j2      # pristine copy, to diff against
diff "$OLD/templates/common/odoorc.j2" ~/.config/ow/templates/common/odoorc.j2
```

No difference: you never touched that file. Delete both copies `--take` wrote
— `~/.config/ow/templates/common/odoorc.j2` and its baseline at
`~/.local/state/ow/template-base/common/odoorc.j2` — and let it come from the
package again. A difference: keep the taken file, but overwrite it with your
`$OLD` version, so the customization survives:

```sh
cp "$OLD/templates/common/odoorc.j2" ~/.config/ow/templates/common/odoorc.j2
```

Repeat for every file under `$OLD/templates/` you suspect you touched.

`--take` writes two copies: yours at
`~/.config/ow/templates/<bundle>/<path>`, which you edit, and a pristine
baseline at `~/.local/state/ow/template-base/<bundle>/<path>`. The baseline is
what lets ow tell you later that it changed the file underneath you —
`ow templates` marks such a file `taken, outdated`, and `ow templates --diff`
shows exactly what moved.

A file you copy in by hand has no baseline. It stays marked `taken` forever and
is never reported as stale. That is the cost of skipping `--take`.

Overrides are per file, not per bundle: taking `common/odoorc.j2` leaves the
rest of `common/` packaged and up to date.

Once you've restored a customization, run `ow apply` on each workspace that
uses it — taking a file changes nothing already materialized until the
workspace is re-applied.

## Services

`services/compose.yml` is packaged inside ow now, but no ow command reads it.
If you were running the stack, keep your copy and go on starting it by hand.
`~/.config/ow/services/` is the conventional place for it:

```sh
mkdir -p ~/.config/ow
cp -r "$OLD/services" ~/.config/ow/
docker compose -f ~/.config/ow/services/compose.yml up -d
```

## `ow rebase` changed

`ow rebase` was rewritten. Three differences will catch anyone with muscle
memory for the old one.

**The prompt defaults to no.** It used to be `Proceed? [Y/n]`, where anything
other than `n` — a bare Enter included — went ahead. It is now
`Proceed? [y/N]`: only `y` or `yes` proceed, and end-of-input aborts. Nothing
happens unless you say so. Pass `-y` in scripts.

**It is idempotent.** Running it twice with nothing changed in between does
nothing the second time. No commit is rewritten.

**It has flags now** — the old one had none:

| Flag | Effect |
|---|---|
| `--only a,b` | restrict the whole run to those repo aliases |
| `--autostash` | stash and restore uncommitted changes around each rebase; without it a dirty worktree is skipped and the run exits non-zero |
| `--dry-run` | fetch, print the plan, touch no worktree |
| `-y`, `--yes` | skip the confirmation prompt |

Nothing is ever pushed, then or now.

## Command renames

| 1.x | 2.0 |
|---|---|
| `ow init` (set up a project) | gone — the global config bootstraps itself |
| `ow create` | `ow init` |
| `ow update` | `ow apply` |
| `ow status` | unchanged |
| `ow rebase` | unchanged in name; see above |
| `ow prune` | unchanged |
| — | `ow ls` — list every known workspace, its path and its repos |
| — | `ow templates` — list, take and diff template files |

`ow -v` is gone: the version flag is now `--version` or `-V`. `-v` almost
everywhere means `--verbose`, and ow shells out to git constantly, so the
short spelling is left free for that.

`ow init` now behaves like `git init`: it creates a workspace in the current
directory, or in `./NAME` if you pass a name. The old `-n/--name` option is
gone — the name is the argument.

`-c/--configuration` is unchanged. `-t/--template` and `-r/--repo` changed shape:

| Flag | 1.x | 2.0 |
|---|---|---|
| `-t/--template` | one `-t`, a space-separated list: `-t common vscode` | one template per `-t`, repeated: `-t common -t vscode` |
| `-r/--repo` | one `-r`, two words: `-r community master..x` | one `-r ALIAS:SPEC`, repeated for more: `-r community:master..x` |

Old habits fail loudly rather than silently doing the wrong thing:
`ow init newone -t common vscode` now errors with
`Got unexpected extra argument (vscode)`, and `ow init -r community master..x`
now errors with `--repo expects ALIAS:SPEC (got 'community')`.

## `OW_WORKSPACE`

It takes exactly one form now: an absolute path to a workspace directory. A
bare workspace name, which 1.x accepted and resolved under
`<root>/workspaces/`, is rejected:

```
Error: OW_WORKSPACE='canary' is not an absolute path
```

`~` is not expanded either. Export the full path — or export nothing at all:
with `OW_WORKSPACE` unset, ow walks up from the current directory to find the
workspace, which is what you want most of the time.

Your existing `mise.toml` files were generated by 1.x and export a name:

```toml
[env]
OW_WORKSPACE = "canary"
```

so every ow command run inside a mise-activated workspace fails on it. The
packaged template now exports mise's `{{config_root}}` — the workspace
directory — instead, so the `ow apply` from step 5 rewrites the file
correctly. Reopen your shell afterwards so mise drops the stale value.

If you had a copy of `common/mise.toml.j2` in 1.x, it wins over the packaged
one and is not rewritten. It also has no baseline, so `ow templates --diff`
will not flag it — see the templates section above. Either add the line by
hand, or move your copy aside, run `ow templates --take common/mise.toml.j2`,
and re-apply your edits on top. The second way also gives you the baseline you
were missing.

## What's left in `$OLD`

Once the above is done, `$OLD/mise.toml` and `$OLD/templates/` are dead —
nothing reads either any more — and safe to delete. `$OLD/ow.toml` is covered
above: keep it or delete it, ow no longer looks for it once the global config
exists.

`$OLD/workspaces/` is only dead once it's empty. Each subdirectory under it is
a real workspace, worktrees and all — moving it out from under `$OLD` is
optional, not required, and one you leave in place is still live and in use,
not inert. Only delete `$OLD/workspaces/` once every workspace under it has
been moved out or is one you no longer need.
