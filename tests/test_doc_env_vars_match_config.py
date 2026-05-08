"""Slice #195: pin doc env-var references to names that
``mnemos/core/config.py`` actually reads (or that the installer
explicitly accepts as a documented alias).

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS):

- ``docs/SPECIFICATION.md`` runtime env section listed
  ``MNEMOS_DB_HOST/PORT/NAME/USER/PASSWORD`` and ``MNEMOS_KEY``.
  Runtime config is ``PG_HOST/PORT/DATABASE/USER/PASSWORD`` and
  the API key is ``MNEMOS_API_KEY`` (``MNEMOS_KEY`` is only the
  name in ``tests/test_live_e2e.py``, not a fleet-config env).
- ``DEPLOYMENT.md`` showed ``PG_POOL_SIZE=50``. Real names are
  ``PG_POOL_MIN`` and ``PG_POOL_MAX``.
- ``docs/OBSERVABILITY.md`` named ``MNEMOS_DB_POOL_MAX_SIZE``;
  the actual cap is ``PG_POOL_MAX``.
- ``docs/OPERATIONS.md`` restore-test command set
  ``MNEMOS_DB_NAME``; should be ``PG_DATABASE``.
- ``docs/MEMORY_ARCHITECTURE.md`` named
  ``MNEMOS_COMPRESSION_ENGINE`` for engine selection. No such
  env var is read anywhere; engine choice runs through the
  contest mechanism, not an env-var override.

Note: ``MNEMOS_DB_*`` is a legitimate INSTALLER alias accepted by
``mnemos/installer/__main__.py`` for env-only deploys; it is NOT
the runtime config shape. This test only guards documents that
describe runtime/operator-facing config.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# Names this test should NEVER find in operator-facing runtime docs.
_FORBIDDEN_RUNTIME_ENV_NAMES = (
    "MNEMOS_DB_POOL_MAX_SIZE",  # not read anywhere
    "PG_POOL_SIZE",             # actual is PG_POOL_MIN / PG_POOL_MAX
    "PG_POOL_TIMEOUT",          # actual is MNEMOS_POOL_ACQUIRE_TIMEOUT
    "MNEMOS_KEY",               # test-only env, not fleet config
    "MNEMOS_COMPRESSION_ENGINE",  # not read anywhere
    # MNEMOS_DB_* names are accepted ALIASES in installer but not
    # the runtime-config shape; operator/runtime docs should
    # describe the runtime canonical (PG_*).
    "MNEMOS_DB_HOST",
    "MNEMOS_DB_PORT",
    "MNEMOS_DB_NAME",
    "MNEMOS_DB_USER",
    "MNEMOS_DB_PASSWORD",
)


def _config_src() -> str:
    return (REPO / "mnemos" / "core" / "config.py").read_text()


def test_pg_pool_min_and_max_are_canonical_in_config():
    """Sanity-check: PG_POOL_MIN and PG_POOL_MAX are the live
    runtime aliases (Field validation_alias). If a future refactor
    consolidates them, this test must be updated AND DEPLOYMENT.md
    + OBSERVABILITY.md need a follow-up."""
    src = _config_src()
    assert 'validation_alias="PG_POOL_MIN"' in src, (
        "PG_POOL_MIN is no longer the validation alias for "
        "_DatabaseSettings.pool_min_size; update doc references "
        "and this test if the env name intentionally changed."
    )
    assert 'validation_alias="PG_POOL_MAX"' in src, (
        "PG_POOL_MAX is no longer the validation alias for "
        "_DatabaseSettings.pool_max_size; update doc references "
        "and this test if the env name intentionally changed."
    )


def test_database_settings_uses_pg_env_prefix():
    """The runtime _DatabaseSettings class must use PG_ as its
    env_prefix — that's what makes PG_HOST/PG_PORT/PG_DATABASE/
    PG_USER/PG_PASSWORD work without explicit `validation_alias`
    annotations on each field. If this changes, doc references
    to these names need a follow-up."""
    src = _config_src()
    assert 'env_prefix="PG_"' in src, (
        "_DatabaseSettings no longer uses env_prefix=\"PG_\". "
        "Update SPECIFICATION.md / DEPLOYMENT.md / "
        "OBSERVABILITY.md / OPERATIONS.md doc references and "
        "this test if the prefix intentionally changed."
    )


def test_canonical_env_names_present_in_config():
    """Pin: each canonical name claimed by the docs is actually
    a `validation_alias` (or `env_prefix`-derived name) in
    `mnemos/core/config.py`. Catches a future refactor that
    renames an alias without updating doc references."""
    src = _config_src()
    canonical = (
        ("MNEMOS_API_KEY", "API key for fleet auth"),
        ("MNEMOS_JUDGE_MODE", "compression judge mode selector"),
        ("MNEMOS_POOL_ACQUIRE_TIMEOUT", "pool-acquire timeout"),
    )
    missing: list[str] = []
    for name, role in canonical:
        if f'validation_alias="{name}"' not in src \
                and f"validation_alias='{name}'" not in src:
            missing.append(f"  {name} ({role})")
    assert not missing, (
        f"{len(missing)} canonical env name(s) no longer present "
        f"in mnemos/core/config.py:\n"
        + "\n".join(missing)
    )


def test_no_forbidden_env_names_in_runtime_docs():
    """Scan operator/runtime docs for env-var names that
    ``mnemos/core/config.py`` does not read. Catches doc drift
    where rename / refactor history left stale names in place."""
    runtime_docs: list[Path] = [
        REPO / "docs" / "SPECIFICATION.md",
        REPO / "docs" / "OPERATIONS.md",
        REPO / "docs" / "OBSERVABILITY.md",
        REPO / "docs" / "MEMORY_ARCHITECTURE.md",
        REPO / "DEPLOYMENT.md",
    ]
    bad: list[str] = []
    for doc in runtime_docs:
        if not doc.exists():
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(),
                                      start=1):
            for forbidden in _FORBIDDEN_RUNTIME_ENV_NAMES:
                if re.search(rf"\b{re.escape(forbidden)}\b", line):
                    bad.append(
                        f"  {doc.relative_to(REPO)}:{lineno}: "
                        f"`{forbidden}` ({line.strip()[:60]})"
                    )
    assert not bad, (
        f"{len(bad)} stale env-var name(s) in runtime docs:\n"
        + "\n".join(bad)
    )
