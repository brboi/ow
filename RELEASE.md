# Release

The version comes from git tags via [setuptools-scm](https://setuptools-scm.readthedocs.io/). It is
computed at build time and written to `src/ow/_version.py` (`write_to` in `pyproject.toml`; the file
is gitignored and absent from a plain checkout).

## Creating a Release

```sh
git tag vX.Y.Z          # the version to release, on the commit to release
git push origin vX.Y.Z
```

The tag push triggers `.github/workflows/release.yml`, which:

1. builds the package with `python -m build`,
2. publishes to PyPI with `pypa/gh-action-pypi-publish`,
3. creates a GitHub release with the `dist/*` artifacts and generated notes.

**Prerequisites:** the `pypi` environment in the repository settings must exist and hold a
`PYPI_API_TOKEN` secret.

Tags are not covered by `.github/workflows/test.yml`: it runs on pull requests and on pushes to
`main` only. Release from a commit whose tests are green.

## Version Format

- Tagged commit: the tag without its `v`. Tag `v3.0.0a1` reports `3.0.0a1`.
- Commits after a tag: the guessed next version, the distance and the commit. 18 commits after
  `v3.0.0a1` reports `3.0.0a2.dev18+g969e1af0f`.
- A source checkout with no generated `_version.py` reports `dev`.

## Check the Version

```sh
ow --version
```
