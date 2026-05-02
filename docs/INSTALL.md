# MNEMOS Install Guide

MNEMOS v5 keeps the memory kernel small by default. Subsystems install through
pip extras, and common deployment shapes are available as named bundles so an
operator picks a deployment shape instead of hand-selecting every subsystem.

## Quick Matrix

| Install | Command | Use it for |
|---|---|---|
| Core | `pip install mnemos-os==5.0.0` | Memory CRUD, search, version DAG, federation, auth/RLS, GRAEAE, MCP, webhooks |
| Edge | `pip install 'mnemos-os[edge]==5.0.0'` | SQLite-only edge devices with `aiosqlite` and `sqlite-vec` |
| Server | `pip install 'mnemos-os[server]==5.0.0'` | Production Postgres deployments with NATS, PERSEPHONE, and PANTHEON |
| ML | `pip install 'mnemos-os[ml]==5.0.0'` | Compression-heavy and dream-state-active deployments |
| Interop | `pip install 'mnemos-os[interop]==5.0.0'` | Cross-platform agent fleets using the KNOSSOS/MemPalace shim |
| Full | `pip install 'mnemos-os[full]==5.0.0'` | All optional MNEMOS subsystems |

For source installs, use the same extras against the editable package:

```bash
python -m pip install -e '.[dev,server,ml]'
```

## Mix And Match

Extras compose normally:

```bash
pip install 'mnemos-os[server,ml]==5.0.0'
pip install 'mnemos-os[edge,interop]==5.0.0'
```

`server,ml` is the usual production-plus-dream-state shape: Postgres + NATS +
PERSEPHONE/PANTHEON plus MORPHEUS/KRONOS/APOLLO/ARTEMIS/hot-path acceleration.

## A La Carte Extras

| Extra | Subsystem | Adds |
|---|---|---|
| `morpheus` | `mnemos/domain/morpheus`, MORPHEUS routes and workers | `numpy>=1.24` |
| `persephone` | PERSEPHONE archival routes and worker | `zstandard>=0.25` |
| `pantheon` | PANTHEON facade routes and IRIS MCP tools | no additional dependency |
| `kronos` | KRONOS admin routes and MCP tools | `numpy>=1.24` |
| `knossos` | KNOSSOS phase-1 stdio/MemPalace shim | no additional dependency |
| `apollo` | APOLLO compression engine | no additional dependency |
| `artemis` | ARTEMIS compression engine | `networkx>=3.3` for TextRank scoring |
| `nats` | NATS substrate and routing-audit consumer | `nats-py>=2.14.0` |
| `hot` | Optional Rust hot-path wheel | `mnemos-hot>=0.1.0` |

Legacy runtime extras that are not subsystem bundles remain available:
`semantic` for fastembed semantic scoring, `gpu` for fastembed CUDA, `phi` for
OpenVINO + fastembed, `tracing`, `structlog`, `docling`, `build`, `sqlite`, and
`dev`.

## Bundle Contents

| Bundle | Expands to |
|---|---|
| `edge` | `aiosqlite`, `sqlite-vec` |
| `server` | `nats`, `persephone`, `pantheon` |
| `ml` | `morpheus`, `kronos`, `apollo`, `artemis`, `hot` |
| `interop` | `knossos` |
| `full` | `morpheus`, `persephone`, `pantheon`, `kronos`, `knossos`, `apollo`, `artemis`, `nats`, `hot`, `edge` |

## Adding An Extra Later

Upgrade the existing environment with the extra and restart MNEMOS:

```bash
pip install 'mnemos-os[persephone]==5.0.0'
systemctl restart mnemos
```

For editable installs:

```bash
python -m pip install -e '.[persephone]'
```

After restart, `mnemos doctor` reports which extras and bundles are installed.
MCP tools for unavailable optional subsystems are not advertised in `tools/list`.

## Migration From Earlier v5 Installs

Before subsystem modularization, `pip install mnemos-os==5.0.0` behaved like an
all-bundled install. After this change it is core-only.

If you were on v5.0.0 and want the old all-bundled behavior:

```bash
pip install 'mnemos-os[full]==5.0.0'
```

If you only need production server features:

```bash
pip install 'mnemos-os[server]==5.0.0'
```

Missing subsystem routes return HTTP 503 with the exact install command. Missing
MCP tools are filtered out of `tools/list`, and optional workers no-op cleanly
when their extra is unavailable.
