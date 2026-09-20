# Generated files

`ow` writes a fixed set of files into a workspace and remembers what it wrote. There is no
template system any more: no bundles, no `.j2` sources, no user override tree. The generators are
plain Python serializers in `ow/utils/generate.py`, the set of outputs is a fixed ordered table,
and ownership is tracked by the SHA-256 of the bytes `ow` last put there.

## The fixed outputs

Nine paths, always the same nine:

| Output | Mode | Written when |
|--------|------|--------------|
| `mise/conf.d/00-ow.toml` | `0644` | always |
| `.vscode/settings.json` | `0644` | always |
| `.zed/settings.json` | `0644` | always |
| `odoorc` | `0600` | a declared repo is the Odoo core |
| `odools.toml` | `0644` | a declared repo is the Odoo core |
| `pyrightconfig.json` | `0644` | a declared repo is the Odoo core |
| `requirements-dev.txt` | `0644` | a declared repo is the Odoo core |
| `.vscode/launch.json` | `0644` | a declared repo is the Odoo core |
| `.zed/debug.json` | `0644` | a declared repo is the Odoo core |

A declared repo is "the Odoo core" when it has `odoo-bin`, `addons/` and `odoo/addons/`
(`is_odoo_main_repo`). Two repos matching the markers is an error, not a guess. The six Odoo-only
outputs appear only for a **supported** major — 18, 19, 20 and their `saas` series. A recognised
core whose major is not supported blocks every write exactly like an invalid or ambiguous one: the
diagnostic names the alias, the series and the supported set, and nothing is written, not even for
the three always-on outputs.

`odoorc` is the only output written `0600`, because it carries the database password and the
admin password.

The mise fragment is `mise/conf.d/00-ow.toml`, never `mise.toml`: `mise` gives `mise.toml` and
`mise.local.toml` higher precedence than anything under `conf.d/`, so those two are yours to keep
permanently. A `mise.toml` written by an ow older than 3.0 shadows the fragment — ow recognises
its own `OW_WORKSPACE` marker, warns, and never deletes the file.

The services compose file is *not* one of these nine: it lives outside any workspace, is not
locked, and is refreshed only by a render with a supported Odoo core. See
[Services](services.md).

## Automatic versus explicit render

A render is one inspection of the workspace plus, when nothing blocks it, one write of what that
inspection owns. Two things trigger it:

- **Automatically.** `ow init` renders the workspace it just created or repaired. `ow switch`,
  `ow pull`, `ow rebase` and `ow reset` refresh the files once after a batch of Git mutations that
  actually executed — never after a dry run, a refusal, a declined prompt, a no-op, or a batch
  where any repo's Git operation failed. `ow mv` and `ow unarchive` refresh at the new path for a
  schema-2 workspace.
- **Explicitly.** `ow render` does it on its own, for the current workspace or one named
  positionally or with `-w`. It is also the command that migrates a schema-1 config — see
  [Migrating to 3.0](migrating-to-3.0.md).

Both paths run the same inspection and the same writes. The difference is only who decides when.

A render refuses to touch anything when the inspection is blocked: a declared worktree is missing
or mid-rebase, the Odoo core is unrecognised, unsupported, invalid or ambiguous, an output would
land inside a declared worktree, or a schema-1 manifest has diagnostics the migration cannot
represent. The blockers are printed and the command exits non-zero without writing a byte. A legacy
root `mise.toml` is a *warning* rather than a blocker — the render still writes, and ow will not
delete a file it did not write — but it makes `ow files --diff` fail the file gate, because the
fragment it shadows is not what mise will actually read.

## Ownership: the rendered lock

`<workspace>/.ow/rendered.lock.toml` records the SHA-256 of the bytes `ow` last wrote — or adopted
as already identical — at each output path. The lock is of *outputs*, not of the sources that
produced them, which is what lets a hand edit survive a generator change.

`ow files` lists every path ow speaks for, in one of six states:

| State | Meaning | What a render does |
|-------|---------|--------------------|
| `absent` | ow would write this path and it is not there | writes it |
| `up to date` | the file already equals what ow would write | adopts it into the lock, no write |
| `outdated` | ow wrote these bytes before, and the current proposal differs | rewrites it |
| `yours` | the file does not match the lock — you changed it | leaves it alone |
| `not rendered` | a locked path no current generator produces, still on disk | leaves it alone |
| `ignored` | the path matches `owignore` | leaves it alone |

Adoption is the bridge from before the lock existed: a file that happens to be byte-identical to
the proposal is taken under management without being rewritten, and the next generator change
updates it like any other owned file.

`yours` is the opt-out. Edit a managed file and ow stops touching it; there is no reconciliation
step and no flag to take it back. A later render that happens to be byte-identical to your file
adopts it back. Deleting a file is *not* an opt-out: a file whose proposal is non-empty comes back
on the next render. A path a generator no longer produces stays `not rendered` while the file is
on disk and is dropped from the listing once you delete it — ow keeps no tombstone.

**ow never deletes a workspace file, and there is no `--force`.** Removing an output from the
generator table retires it; it does not remove it. To get rid of a generated file, delete it
yourself.

### Ignoring paths

`owignore` in the global config is a list of gitignore-style patterns:

```toml
owignore = [".zed/**", "!.zed/settings.json"]
```

A pattern matching an output path keeps ow from writing it, and the path is listed as `ignored`.
The syntax is `pathspec`'s gitignore dialect, so a directory pattern such as `.zed/**` covers
everything under that directory and a leading `!` re-includes a path. An ignored path that
already has a lock entry keeps it: ignoring is
not retiring, and un-ignoring restores the file to ow's ownership with the same bytes.

`.ow/config.toml` and `.ow/rendered.lock.toml` are reserved: a generator proposing either is a
blocking error. So is an output whose path is absolute, escapes the workspace, or whose parent
directory is a symlink.

## `ow files` and `ow files --diff`

`ow files` lists the paths and their states. It writes nothing, fetches nothing and migrates
nothing, and it exits `1` only when the inspection is blocked (missing worktree, migration
diagnostics, legacy shadowing, an unsafe output). Ordinary `yours` differences are a normal state,
not a failure, and exit `0`.

`ow files --diff` prints a unified diff for every path that differs — from your file to what ow
would write, with an addition diffed from `/dev/null`, and a file whose bytes are not UTF-8 named
with a one-line reason instead of a diff. It writes nothing, and it exits:

- `0` when every path is `up to date`, `not rendered` or `ignored`;
- `1` when any path is `yours`, `outdated` or `absent`, or when the inspection is blocked.

The Git half of the same question is `ow status`: file alignment is not Git-drift alignment.
`ow files` says nothing about branches, commits or remotes, and `ow status` never lists a path or
prints a diff — it shows the same inspection's *diagnostics* (errors, blockers and warnings) plus a
`N pending difference(s)` count that points back at `ow files` for the listing and the diff. A repo
whose branch drifted from its spec is realigned by `ow switch`; a missing worktree or bare repo is
repaired by `ow init`; the files are `ow render`'s business.
