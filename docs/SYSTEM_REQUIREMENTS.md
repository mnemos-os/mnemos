# System Requirements

Reference for operators planning a MNEMOS deployment. Covers the
resource floor for each of the operating modes that the v3.x line supports
today, plus what drops off at each tier.

Profiles are descriptive sizing tiers. The feature set is controlled by
the install profile plus individual env vars in the "Environment knobs"
section at the bottom.

## Tiers at a glance

| Tier          | CPU     | RAM    | Disk (data) | GPU        | Notes                                              |
| ------------- | ------- | ------ | ----------- | ---------- | -------------------------------------------------- |
| **Server**    | 8+ cores| 16 GB+ | 50 GB+ SSD  | CUDA 12+ GPU w/ 8 GB+ VRAM (recommended) | Full contest path (APOLLO + ARTEMIS); Postgres 15+ on same host or nearby |
| **Workstation** | 4+ cores | 8 GB  | 20 GB SSD  | Optional GPU (4 GB+ VRAM acceptable)     | Full contest path; APOLLO LLM fallback uses GPU when configured |
| **Edge**      | 2 cores | 4 GB   | 10 GB       | None       | Contest path disabled via `MNEMOS_CONTEST_ENABLED=false`; compression queue is not drained |

Embedded Pi-class is now a **lite-profile target** (SQLite + sqlite-vec
backend) and is out of scope for the current Postgres-only branch. Pi 4 class is the intended
floor for the embedded tier when it lands.

## Baseline requirements (all tiers)

* **Python**: 3.11+ (`tomllib` stdlib dependency)
* **Postgres**: 15+ with `pgvector` extension. Either co-located or on
  a local network (latency < 5 ms for the worker's dequeue path to
  keep up with production ingest).
* **Disk**: corpus + manifests + backups.
  - Memory text: ~1 KB/row average; 100k rows ≈ 100 MB.
  - v3.1 compression candidates: ~1.5x the memory row count, ~2 KB/row.
  - Backups: see `tools/backup/` — daily pg_dump + weekly rsync
    pattern uses another ~2x the live corpus size in rolling storage.
* **Network**: internal only for the v3.1 contest path; outbound only
  required if using an externally hosted embedding/LLM endpoint.

## Server tier — full v3.1 feature set

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
* **Ancillary**: Redis/memcached NOT required — the contest
  path is single-worker per the DEPLOYMENT scaling note. Multi-worker
  coordination is v3.2.

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

## v3.5.x deployment note

Docker fresh volumes run all mounted initdb migrations. Existing volumes
do not. For v3.5.x, the compose files include a one-shot
`postgres-upgrade` service so the trigger replacement migration applies
to existing data directories before MNEMOS starts.

---

*Last updated: 2026-04-28 (v3.5.1 doc triage)*
