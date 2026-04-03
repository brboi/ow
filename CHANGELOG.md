# Changelog

## Unreleased

### Added

- Debug configuration for OW itself (`.vscode/launch.json`) — allows debugging `ow` CLI commands
- Automatic versioning via `setuptools-scm` based on Git tags
- `ow --version` flag to display current version

## 2026-04-03

### Changed

- Removed `parallel_fetch` — fetches now run sequentially with spinner feedback
- Improved git command output format: `[<alias>.git] <cmd> <args>`
- Errors are now always reported, never silently ignored

### Breaking Changes

- **Debug templates:** `vars.test_tags` is no longer used in debug launch configs (VSCode `launch.json`, Zed `debug.json`). Replace with:
  - `vars.debug_test_args = ["--test-tags=/your_tag"]` for custom test tags
  - If omitted, defaults to `["--test-tags=<workspace_name>"]`
- **Debug templates:** The test debug config label changed from `"Debug Tests With Tags: <tag>"` to `"Debug Tests (<workspace_name>)"`.
- **Debug templates:** Run Instance args are now configurable via `vars.debug_args`. Defaults to `["--dev=all", "--with-demo"]`. For Odoo <= 18.0, set `vars.debug_args = ["--dev=all"]` to omit `--with-demo`.

### Added

- `vars.debug_args` — array of CLI arguments for the "Run Instance With Debug" config (default: `["--dev=all", "--with-demo"]`)
- `vars.debug_test_args` — array of CLI arguments for the "Debug Tests" config (default: `["--test-tags=<workspace_name>"]`)
- `format_workspace` now correctly serializes list-type vars as valid TOML arrays
