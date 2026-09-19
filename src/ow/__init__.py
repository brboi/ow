"""ow — Odoo workspace manager.

`_version.py` is written by setuptools-scm at build time and gitignored, so
it is there in an installed ow and absent from a plain source checkout. The
fallback belongs here, once: every caller reads `ow.__version__` rather than
reaching for the generated module and guessing what to do when it is missing.
"""

try:
    from ow._version import version as __version__
except ImportError:  # a source checkout, not an install
    __version__ = "dev"
