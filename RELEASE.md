# Release

Version is auto-generated from Git tags using [setuptools-scm](https://setuptools-scm.readthedocs.io/).

## Creating a Release

```bash
git tag v0.2.0
git push origin v0.2.0
```

That's it. The `release.yml` GitHub Actions workflow triggers automatically and:

1. Builds the package
2. Publishes to PyPI
3. Creates a GitHub release with auto-generated notes

**Prerequisites:** the `pypi` environment in the GitHub repository settings must exist and have a `PYPI_API_TOKEN` secret configured.

## Version Format

- Development: `0.1.dev46+gabc123`
- Release: `0.2.0` (from tag `v0.2.0`)

## Check Version

```bash
ow --version
```
