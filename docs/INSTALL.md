# MNEMOS Install Guide

MNEMOS v5 keeps the memory kernel small by default. Subsystems install through
pip extras, and common deployment shapes are available as named bundles so an
operator picks a deployment shape instead of hand-selecting every subsystem.

## Quick Matrix

| Install | Command | Use it for |
|---|---|---|
| Core | `pip install mnemos-os==5.0.1` | Memory CRUD, search, version DAG, federation, auth/RLS, GRAEAE, MCP, webhooks |
| Edge | `pip install 'mnemos-os[edge]==5.0.1'` | SQLite-only edge devices with `aiosqlite` and `sqlite-vec` |
| Server | `pip install 'mnemos-os[server]==5.0.1'` | Production Postgres deployments with NATS, PERSEPHONE, and PANTHEON |
| ML | `pip install 'mnemos-os[ml]==5.0.1'` | Compression-heavy and dream-state-active deployments |
| Interop | `pip install 'mnemos-os[interop]==5.0.1'` | Cross-platform agent fleets using the KNOSSOS/MemPalace shim |
| Full | `pip install 'mnemos-os[full]==5.0.1'` | All optional MNEMOS subsystems |

For source installs, use the same extras against the editable package:

```bash
python -m pip install -e '.[dev,server,ml]'
```

## Mix And Match

Extras compose normally:

```bash
pip install 'mnemos-os[server,ml]==5.0.1'
pip install 'mnemos-os[edge,interop]==5.0.1'
```

`server,ml` is the usual production-plus-dream-state shape: Postgres + NATS +
PERSEPHONE/PANTHEON plus MORPHEUS/KRONOS/APOLLO/ARTEMIS/hot-path acceleration.

## A La Carte Extras

| Extra | Subsystem | Adds |
|---|---|---|
| `build` | PyInstaller build support | `pyinstaller>=6.0`, `sqlite-vec` |
| `docling` | Document parsing/import support | `docling>=2.5.0`, `docling-core>=2.0.0`, `pillow>=10.0.0` |
| `tracing` | OpenTelemetry tracing | `opentelemetry-api>=1.27.0`, `opentelemetry-sdk>=1.27.0`, `opentelemetry-exporter-otlp-proto-http>=1.27.0` |
| `structlog` | Structured JSON logging | `structlog>=25.0.0` |
| `sqlite` | SQLite persistence support | `aiosqlite>=0.20.0`, `sqlite-vec>=0.1.6` |
| `morpheus` | `mnemos/domain/morpheus`, MORPHEUS routes and workers | `numpy>=1.24` |
| `persephone` | PERSEPHONE archival routes and worker | `zstandard>=0.25` |
| `pantheon` | PANTHEON facade routes and IRIS MCP tools | no additional dependency |
| `kronos` | KRONOS admin routes and MCP tools | `numpy>=1.24` |
| `kronos-gpu` | KRONOS GPU acceleration | `cupy>=12` |
| `knossos` | KNOSSOS phase-1 stdio/MemPalace shim | no additional dependency |
| `apollo` | APOLLO compression engine | no additional dependency |
| `artemis` | ARTEMIS compression engine | `networkx>=3.3`, `scipy>=1.11` |
| `nats` | NATS substrate and routing-audit consumer | `nats-py>=2.14.0` |
| `hot` | Optional Rust hot-path wheel | `mnemos-hot>=0.2.0` |
| `edge` | Edge deployment bundle | `aiosqlite>=0.20.0`, `sqlite-vec>=0.1.6` |
| `server` | Server deployment bundle | `mnemos-os[nats,persephone,pantheon]` |
| `ml` | ML deployment bundle | `mnemos-os[morpheus,kronos,apollo,artemis,hot]` |
| `interop` | Interop deployment bundle | `mnemos-os[knossos]` |
| `full` | Full deployment bundle | `mnemos-os[morpheus,persephone,pantheon,kronos,knossos,apollo,artemis,nats,hot,edge]` |
| `semantic` | CPU semantic scoring | `fastembed>=0.3.0` |
| `gpu` | NVIDIA CUDA semantic scoring | `fastembed-gpu>=0.3.0` |
| `phi` | Intel iGPU semantic scoring | `openvino-genai>=2024.4.0`, `fastembed>=0.3.0` |
| `dev` | Development/test tooling | `import-linter>=2.0.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`, `pytest-cov>=5.0.0`, `ruff>=0.5.0` |

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
pip install 'mnemos-os[persephone]==5.0.1'
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
pip install 'mnemos-os[full]==5.0.1'
```

If you only need production server features:

```bash
pip install 'mnemos-os[server]==5.0.1'
```

Missing subsystem routes return HTTP 503 with the exact install command. Missing
MCP tools are filtered out of `tools/list`, and optional workers no-op cleanly
when their extra is unavailable.
