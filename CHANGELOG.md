# Changelog

All notable changes to ow. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [PEP 440](https://peps.python.org/pep-0440/): `3.0.0b1` is a pre-release.

## Unreleased

## 3.0.0b1

ow 3.0 is a rewrite of ow 2.x around a generic worktree core, a Workspace + Stack model and plugins. **Read [Migrating to 3.0](docs/migrating-to-3.0.md) first.** It lists what to run, what to delete and what to script differently.

This is a beta. The [plugin API v1](docs/plugins.md#versioning-and-compatibility) may still change, and [`ow sandbox`](docs/commands.md#ow-sandbox) is experimental.

### Breaking changes

- ow 2.x is gone: the TUI, templates (`[templates]`, `[vars]`), `ow mv`, `ow archive`, `ow unarchive`, `ow switch`, `ow pull`, `ow reset`, `ow fetch` and `ow open`. The [migration page](docs/migrating-to-3.0.md#commands-that-changed) maps each one.
- Linux only. Requirements are in the [README](README.md#requirements).
- `mise.python` and `mise.shared_venv` are `python.version` and `python.shared_venv`. A config with the old keys is refused.
- `--branch` is gone from `ow init` and `ow add`. Name the base in the spec: `@18.0..18.0-fix` ([Specs](docs/commands.md#specs)).
- `ow run` starts shared PostgreSQL, pgweb and Mailpit containers. The 2.x compose stack is no longer read ([Shared services](docs/configuration.md#shared-services)).
- No `--json` on any command.
- `ow run`, `ow exec` and `ow sandbox` return the child's exit code ([Exit codes](docs/commands.md#exit-codes)).
- `ow trust` without a terminal needs `-y`.
- A workspace is `WS`, a spec is `SPEC`, and `--as NAME` is `--as STACK`.

### Added

- **Workspaces and stacks**: mono-stack, or multi-stack with coordinated ports and a generated `<workspace>.code-workspace`. `ow init` and `ow add` print the whole plan before they act and undo a failed unit ([`ow init`](docs/commands.md#ow-init)).
- **Specs**: PRs with sister PRs, Runbot URLs, `<repo>@<ref>`, `<repo>@<base>..<branch>`, `@<base>` ([Specs](docs/commands.md#specs)).
- **Upstreams**: a PR branch tracks its head branch, on `origin` or on a fork remote. A branch never pushed is set up so the first `git push` creates it ([Bases and upstreams](docs/commands.md#bases-and-upstreams)).
- **`ow rebase`** puts each branch back on its base and takes in a colleague's commits. `ow rebase BASE` changes the series ([`ow rebase`](docs/commands.md#ow-rebase)).
- **`ow run`**: foreground with the real terminal, parallel multi-stack runs, `-d` detached runs with `ow stop` and `ow logs`, `--tmp` throwaway workspaces, `--reset`. A runner that detaches itself is followed in the foreground and stopped on exit ([`ow run`](docs/commands.md#ow-run)).
- **`ow exec`**, **`ow status`**, **`ow files`**, **`ow trust`**, **`ow refresh`**.
- **`ow sandbox`** (experimental): bubblewrap, with profiles for common AI agent harnesses.
- **Shared Python virtualenvs** and generated `odoorc`, `odools.toml`, `pyrightconfig.json`, VS Code and Zed settings that never overwrite a hand edit ([Generated files](docs/files.md)).
- **Shared services** and unique database names ([Configuration](docs/configuration.md)).
- **Plugins**: built-in ones, and plugins shipped inside a repo, loaded after `ow trust` ([Plugins](docs/plugins.md)).

### Changed

- Config rewrites keep keys they do not know. Only `ow config unset` removes one ([Unknown keys](docs/configuration.md#unknown-keys-and-wrong-types)).
- Container images are pinned: `sosedoff/pgweb:0.17.0` and `axllent/mailpit:v1.34.0`.
- `ow rm` and `ow prune` refuse a workspace with a running process. Each `ow run -d` starts a fresh log, and `ow logs -f` stops when the process dies.
- Ctrl+C on a foreground `ow run`, `ow exec`, `ow sandbox` or `ow stop` lets the child tear down. A second one terminates it. The exit code is 130.
- A detached process whose start time is unknown is not treated as ours.

### Security

- A moved workspace is repaired, never recreated. ow removes a worktree only when it is provably empty, locked as `initializing`, or clean.
- The `postgres` superuser has a random password, passed to the container through a `0600` env file. The `odoo` role is still reachable on every interface by default: [Security: Postgres](docs/configuration.md#security-postgres).
- In-repo plugins run only from a location you trusted. A corrupt trust file is an error and is never rewritten ([Trust](docs/plugins.md#trust)).
- `ow sandbox` mounts `commondir`, `gitdir`, the lock's files, the approved plugin files and `.ow/` read-only, and documents what it does not protect ([Residual risks](docs/commands.md#residual-risks)).

### Release

- `release.yml` publishes only after the tests pass. CI tests git 2.45.4, the minimum, on Ubuntu and Alpine ([RELEASE.md](RELEASE.md)).

[Unreleased]: https://github.com/brboi/ow/compare/v3.0.0b1...HEAD
[3.0.0b1]: https://github.com/brboi/ow/compare/v3.0.0a1...v3.0.0b1
