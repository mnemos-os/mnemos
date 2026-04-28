"""Single source of truth for the MNEMOS package version.

pyproject.toml and this file MUST stay in sync. Bump both in the same
commit; the release script reads them and refuses to ship if they
disagree.

Why a separate file rather than `from importlib.metadata import
version`: that path returns whatever pip last installed, which on an
editable container goes stale the moment `pyproject.toml` is bumped
without a re-install. A literal Python constant is what every runtime
caller imports — no install-state coupling.
"""

__version__ = "3.5.1"
