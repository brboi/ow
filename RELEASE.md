# Release

The version comes from git tags via [setuptools-scm](https://setuptools-scm.readthedocs.io/). It is
computed at build time and written to `src/ow/_version.py` (`write_to` in `pyproject.toml`; the file
is gitignored and absent from a plain checkout).

## Creating a Release

1. Update [CHANGELOG.md](CHANGELOG.md): move what is under `## Unreleased` (add the heading if it is
   missing) into a new `## X.Y.Z` section, breaking changes first. Commit it.
2. Tag the commit to release and push the tag:

```sh
git tag vX.Y.Z          # e.g. v3.0.0b1, v3.0.0rc1, v3.0.0
git push origin vX.Y.Z
```

The tag push triggers `.github/workflows/release.yml`, which:

1. builds the package with `python -m build`,
2. publishes to PyPI with `pypa/gh-action-pypi-publish`,
3. creates a GitHub release with the `dist/*` artifacts and generated notes. A tag whose name
   contains `a`, `b` or `rc` (`v3.0.0a1`, `v3.0.0b1`, `v3.0.0rc1`) is published as a GitHub
   **pre-release**; PyPI hides such versions from a plain `pip install`.

**Prerequisites:** the `pypi` environment in the repository settings must exist and hold a
`PYPI_API_TOKEN` secret.

Tags are not covered by `.github/workflows/test.yml`: it runs on pull requests and on pushes to
`main` only. Release from a commit whose tests are green.

## Version Format

- Tagged commit: the tag without its `v`. Tag `v3.0.0b1` reports `3.0.0b1`.
- Commits after a tag: the guessed next version, the distance and the commit. 18 commits after
  `v3.0.0a1` reports `3.0.0a2.dev18+g969e1af0f`.
- A source checkout with no generated `_version.py` reports `dev`.

## Check the Version

```sh
ow --version
```
