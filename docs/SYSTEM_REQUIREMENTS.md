# System Requirements

Reference for operators planning a MNEMOS deployment. Covers the
resource floor for each of the operating modes that the v5.3 line supports
today, plus what drops off at each tier.

Profiles are descriptive sizing tiers. The feature set is controlled by
the install profile plus individual env vars in the "Environment knobs"
section at the bottom.

## Tiers at a glance

| Tier          | CPU     | RAM    | Disk (data) | GPU        | Notes                                              |
| ------------- | ------- | ------ | ----------- | ---------- | -------------------------------------------------- |
| **Server**    | 8+ cores| 16 GB+ | 50 GB+ SSD  | CUDA 12+ GPU w/ 8 GB+ VRAM (recommended) | Postgres + pgvector + Redis; multi-worker supported |
| **Workstation** | 4+ cores | 8 GB  | 20 GB SSD  | Optional GPU (4 GB+ VRAM acceptable)     | `dev` profile with SQLite, or `server` profile for local Postgres |
| **Edge**      | 2 cores | 4 GB   | 10 GB       | None       | SQLite + sqlite-vec single-worker profile |

Embedded Pi-class is now the **edge** profile target (SQLite + sqlite-vec
backend). Pi 4 class is the intended floor for the embedded tier.

## Baseline requirements (all tiers)

* **Python**: 3.11+ (`tomllib` stdlib dependency)
* **Database**: SQLite for `edge`/`dev`; Postgres 15+ with `pgvector`
  extension for `server`. Server multi-worker deployments also need Redis.
* **Disk**: corpus + manifests + backups.
  - Memory text: ~1 KB/row average; 100k rows ≈ 100 MB.
  - v3.1 compression candidates: ~1.5x the memory row count, ~2 KB/row.
  - Backups: see `mnemos/tools/backup/` — daily pg_dump + weekly rsync
    pattern uses another ~2x the live corpus size in rolling storage.
* **Network**: internal only for the contest path; outbound only
  required if using an externally hosted embedding/LLM endpoint.

## Server tier — full v5.3 feature set

Intended for the primary deployment host that runs the API + worker
for production ingest.

* **CPU**: 8+ cores (4 for API, 2+ for worker, headroom for Postgres
  if co-located).
* **RAM**: 16 GB minimum. Postgres tuned for the working-set size
  of the memories + candidates tables + indexes. `shared_buffers`
  ≈ 25% of RAM is a fine default.
* **Disk**: 50 GB+ SSD for a year of daily ops at moderate ingest
  (~10k memories/day). NVMe strongly preferred — the v3_dag manifest
  writes are write-heavy.
* **GPU**:
  - **Recommended**: NVIDIA RTX 4000-class or better, 8 GB+ VRAM, CUDA 12+.
    APOLLO's schema-aware fast path is CPU-cheap; GPU is only needed
    for APOLLO's optional LLM fallback and judge-LLM scoring.
  - **Sufficient**: any CUDA-capable GPU with enough VRAM to load
    the chosen embedding/LLM model. The default models (see
    `CLAUDE.md` at the repo root) fit on 8 GB.
* **Ancillary**: Redis is not required for the default single-worker
  deployment. Redis is required for multi-worker shared rate-limit and
  circuit-breaker state; see `docs/SCALING.md`.

## Workstation tier — full feature set, CPU-only acceptable

Dev machines, solo researchers, small teams.

* **CPU**: 4+ cores. ARTEMIS runs locally on CPU.
* **RAM**: 8 GB. CPU-only inference loads the full embedding model into
  RAM; 8 GB is the comfortable floor.
* **Disk**: 20 GB SSD for mid-scale personal corpora.
* **GPU**: optional. A 4 GB VRAM GPU is enough for APOLLO's LLM fallback
  if you accept longer ingest latency on schema-less content.

## Edge tier — contest disabled

Minimal deployments: a Jetson Orin Nano or similar x86 edge node
running the API without compression queue draining. No multi-engine
contest, no GPU required.

* **CPU**: 2 cores.
* **RAM**: 4 GB. Postgres + Python API server + worker fit here;
  leave 1 GB headroom for the OS.
* **Disk**: 10 GB for the corpus + rolling 7-day backup.
* **GPU**: explicitly none. Set `MNEMOS_CONTEST_ENABLED=false` to skip
  registering the contest engines.
* **Features dropped**:
  - contest path (multi-engine compression)
  - APOLLO's LLM fallback and judge-LLM scoring
  - scoring profiles (N/A without contest)
  - memory_compression_candidates / memory_compressed_variants tables
    migrate cleanly but stay empty

## Environment knobs (v3.x)

These env vars control which features a running worker will exercise.
Defaults are the server-tier shape.

| Env var                                | Default  | Purpose                                                             |
| -------------------------------------- | -------- | ------------------------------------------------------------------- |
| `MNEMOS_CONTEST_ENABLED`               | `true`   | Toggle the contest path                                             |
| `MNEMOS_CONTEST_MIN_CONTENT_LENGTH`    | `0`      | Skip contests for memories shorter than N chars (GPU-constrained installs) |
| `MNEMOS_CONTEST_STALE_THRESHOLD_SECS`  | `600`    | Stale-running queue-row reclaim threshold (v3.1.1)                  |

Set them via the service-unit environment file or `docker run -e …`.

## Observed resource usage (v3.1)

From real deployments as of 2026-04-23:

| Host      | Tier        | CPU avg  | RAM resident | Disk (live) | GPU util                     |
| --------- | ----------- | -------- | ------------ | ----------- | ---------------------------- |
| PYTHIA    | Server      | ~15% of 12 cores | ~8 GB (pg + api + worker) | ~12 GB (corpus 5k+ memories, backups separate) | N/A (no GPU; offloads to CERBERUS) |
| CERBERUS  | Server + GPU | ~20% of 24 cores | ~18 GB (pg + api + worker + vLLM) | ~30 GB | 60-80% during active contest, idle otherwise |

These are operational rather than prescriptive — real workloads will
differ. Use these as a sanity check when sizing a new host.

## Deployment note

Docker fresh volumes run all mounted initdb migrations. Existing volumes
do not. The compose files include a one-shot `postgres-upgrade` service so the
ordered migration tail applies to existing data directories before MNEMOS
starts.

## Platform and package prerequisites

Supported operator targets are Linux, macOS, Windows through WSL2, and BSD-style
systems where Python and database dependencies are available. Ubuntu 22.04+,
Ubuntu 24.04+, and Debian 12 remain the most tested Linux bases.

For bare-metal installs, provide a compiler toolchain, OpenSSL headers, Git,
curl, and the PostgreSQL client/development libraries when using the server
profile. macOS operators can install the equivalent packages with Homebrew:
`postgresql`, `libpq`, `openssl`, and `git`.

## Docker and container resources

Docker Engine 20.10+ is the minimum supported container runtime; Docker 24+ and
Compose v2 are recommended. Allocate at least 4 GB RAM and 2 CPU cores for a
small local stack, 8 GB RAM and 4 CPU cores for routine server testing, and
16 GB+ RAM with 8+ CPU cores for production-like multi-service stacks.

The API listens on `5002` by default. MCP HTTP/SSE commonly uses `5004`.
PostgreSQL uses `5432`, Redis uses `6379`, and reverse proxies should terminate
TLS before forwarding to MNEMOS. Outbound network access is only required for
the LLM, embedding, webhook, federation, or package-index services that the
operator explicitly enables.

## Provider and optional component requirements

At least one model provider is required for GRAEAE reasoning. Supported
deployment shapes include hosted provider APIs, local Ollama, or local vLLM.
Ollama requires enough RAM to hold the selected model; vLLM generally requires
CUDA-capable GPU capacity sized for the model being served.

Redis is required for multi-worker shared rate-limit and circuit-breaker state.
It is optional for single-worker development and edge deployments. Local
embedding, local inference, and GPU-backed APOLLO fallback are optional
capabilities rather than baseline API requirements.

## Production checklist

Before exposing a server deployment, confirm:

* CPU, memory, disk, and backup capacity match the expected corpus size.
* PostgreSQL, pgvector, Redis, and Docker or systemd units are installed as
  required by the selected profile.
* API keys, OAuth session secrets, provider credentials, rate limiting, backup
  destinations, and monitoring are configured.
* Health checks pass on `/health`, model/provider checks pass through the CLI,
  and the backup and restore path has been tested.

## Scaling and cloud guidance

Scale vertically first: memory and disk I/O usually become visible before CPU
for normal memory workloads, while GRAEAE consultation throughput depends on
provider latency and concurrency limits. Scale horizontally only with shared
Redis state and an external PostgreSQL writer endpoint. See `docs/SCALING.md`
for the multi-worker contract.

For cloud deployments, choose general-purpose instances with SSD storage and
managed PostgreSQL where practical. Small deployments can run on a 2-4 vCPU
host with 8 GB RAM; larger deployments should separate Postgres storage from
API workers and size disk for corpus growth plus rolling backups.

## Troubleshooting and verification

Common first checks:

* `python3 --version` reports Python 3.11+.
* `psql --version` reports a supported PostgreSQL client when using `server`.
* Docker and Compose report supported versions for container deployments.
* `mnemos doctor` passes after initialization.
* `curl http://localhost:5002/health` returns healthy service state.

---

*Last updated: 2026-05-08 (v5.0.1 doc sync)*
