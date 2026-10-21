# PROTOTYPE — a TUI as an ow 3 plugin (throwaway)

Answers [Does the plugin interface carry a TUI? (throwaway TUI-plugin POC)](https://github.com/brboi/ow/issues/75).
Not production code; never merge.

| File | What |
|---|---|
| `ow3.py` | stub ow 3 core: the #69 hookspecs (pluggy), `ls`, `status`, `reset` per #71/#72, one fixed plugin tuple |
| `tui.py` | the dashboard plugin: one hook (`ow_commands` → `ow tui`), everything else through the CLI as a child process |
| `mkfleet.sh` | scratch fleet in `$OW3_PROTO_HOME` (default `/tmp/ow3-proto-WIPE-ME`) |
| `drive.py` | headless Textual pilot: lists, shows status, confirms and runs `reset`, while a collaborator pushes mid-dialog |
| `shots/` | screenshots from `drive.py` |

Run: `./mkfleet.sh && python ow3.py tui` (interactive) or `./mkfleet.sh && python drive.py` (headless).

## How the dashboard talks to ow

| Need | Call |
|---|---|
| workspace list | `ow ls` (tab-separated human output, parsed) |
| status | `ow status -w <ws>`, shown verbatim |
| mutating command | `ow reset -w <ws> -n` → dialog shows the plan → `ow reset -w <ws> -y --no-fetch`, streamed into the log |

Children run with `stdin=DEVNULL`, `start_new_session=True`, `GIT_TERMINAL_PROMPT=0`: they can neither prompt
nor touch the dashboard's terminal. Exit codes 0/1/2 (#72) are enough to tell done / refused-or-failed / declined.

## Observed (`python drive.py`)

```
[a-no-fetch] second run --no-fetch: confirmed e8a678f89d, upstream now 8511c218a2, community HEAD e8a678f89d -> MATCHES what was confirmed
[b-refetch] second run refetches: confirmed 8511c218a2, upstream now f72a1f7382, community HEAD f72a1f7382 -> DIFFERS from what was confirmed
```

## Findings

1. **No hook was added.** `ow_commands(app)` is the only part of the plugin interface the dashboard uses, and it
   needs nothing else from it: contributions are pure functions of one `Workspace`, which is not what a
   fleet view wants. Being a plugin buys the `ow tui` entry point and nothing more; the same code would
   run as a separate program.
2. **Confirm-then-run works over the CLI** as `-n` (fetches, prints the plan) then `-y --no-fetch` (re-plans
   from the same cached refs). Rerunning `-y` *with* a fetch broke "what was confirmed is what runs" when the
   upstream moved during the dialog (case b). The spec must therefore pin: `-n` runs the fetch, and every
   command that fetches takes `--no-fetch`. Residual window: a worktree changed between the two runs (a
   commit from another terminal) gives the same command text but a different effect.
3. **Parsing human output is the awkward part.** `status` shown verbatim is fine; `ls` must be parsed to get
   names, and a tab or newline in a path breaks it.
4. **Credentials are the unexercised risk.** A child with no terminal cannot ask for an SSH passphrase
   (ow 2's askpass broker exists for exactly this, issue #51). A dashboard would have to supply its own
   `SSH_ASKPASS`, which only works if ow 3 yields to an inherited one. Not built here.
5. Lost on the way: colours (rich strips ANSI when stdout is not a TTY). Cosmetic.
