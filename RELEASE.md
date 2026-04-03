# Release

Version is auto-generated from Git tags using [setuptools-scm](https://setuptools-scm.readthedocs.io/).

## Creating a Release

```bash
git tag v0.2.0
git push origin v0.2.0
```

Optional: create a GitHub release

```bash
gh release create v0.2.0 --generate-notes
```

That's it. No need to edit `pyproject.toml`.

## Version Format

- Development: `0.1.dev46+gabc123`
- Release: `0.2.0` (from tag `v0.2.0`)

## Check Version

```bash
ow --version
```
