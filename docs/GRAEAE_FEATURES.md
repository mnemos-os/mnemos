# GRAEAE Feature Surface

**Status:** current v4.0.0 documentation. v4.0.0 shipped on 2026-04-29.

GRAEAE is MNEMOS's multi-provider reasoning bus. It fans a prompt out to live
LLM providers, scores the responses, persists consultations, and writes a
hash-chained audit record for each committed consultation.

## Runtime Modules

| Capability | Current module | Notes |
|---|---|---|
| Provider routing | `mnemos/domain/graeae/engine.py` | Config-driven providers with OpenAI-compatible, Anthropic, and Gemini adapter paths. |
| Circuit breaking | `mnemos/domain/graeae/_circuit_breaker.py` | Per-provider CLOSED / OPEN / HALF_OPEN guard; Redis-backed in server multi-worker mode, in-process fallback otherwise. |
| Rate limiting | `mnemos/domain/graeae/_rate_limiter.py` | Sliding-window requests-per-minute guard; Redis-backed when configured. |
| Concurrency limiting | `mnemos/domain/graeae/_concurrency.py` | Per-provider async slot limiter; Redis-backed when configured. |
| Quality weighting | `mnemos/domain/graeae/_quality.py` | Rolling success-rate and latency tracker used to adjust provider weights. |
| Response cache | `mnemos/domain/graeae/_cache.py` | In-memory normalized prompt cache with TTL; not a persistent semantic cache. |
| Model registry sync | `mnemos/domain/graeae/provider_sync.py`, `mnemos/domain/graeae/elo_sync.py`, `scripts/sync_provider_models.py` | Provider model discovery plus Arena.ai/LMArena weighting when the sync job is installed. |

The reliability state above uses Redis in the `server` profile when
`RATE_LIMIT_STORAGE_URI=redis://...` is configured. In-process fallback remains
for `edge`/`dev` and logs a warning when used with multiple workers.

## API Surface

- `POST /v1/consultations` — run a consultation and persist the consensus,
  provider responses, cost, latency, and optional memory references.
- `GET /v1/consultations/{id}` — retrieve one consultation visible to the
  caller.
- `GET /v1/consultations/{id}/artifacts` — retrieve stored request/response
  artifacts for one consultation.
- `GET /v1/consultations/audit` — list GRAEAE audit rows; non-root callers are
  scoped to their own consultation rows, root can inspect globally.
- `GET /v1/consultations/audit/verify` — verify the visible hash chain. Root
  verifies the full global chain; non-root verifies the caller-scoped view.
- `GET /v1/consultations/muses` and `GET /v1/consultations/modes` — expose the
  configured reasoning options.
- `GET /v1/providers`, `GET /v1/providers/health`, and
  `GET /v1/providers/recommend` — provider inventory, status, and cost-aware
  recommendation.

## Consultation Modes

`POST /v1/consultations` accepts seven modes. Unknown modes are rejected by
request validation with HTTP 422.

| Mode | Shape | Semantics |
|---|---|---|
| `auto` | Routing strategy | Use the engine's default routing for the task type. |
| `local` | Routing strategy | Force local-only muses where configured. |
| `external` | Routing strategy | Force external commercial muses where configured. |
| `all` | Routing strategy | Fan out to every available muse. |
| `single` | Reasoning shape | Pick exactly one highest-weighted muse; use for fast, low-stakes, or cost-floor consultations. |
| `debate` | Reasoning shape | Run a two-round cross-muse argument: round 1 fans out, round 2 gives each muse the others' responses for refinement. |
| `majority` | Reasoning shape | Query up to three muses and report whether pairwise similarity reached the quorum threshold. If quorum is missed, responses still return with `quorum_reached: false`. |

OpenAI-compatible access is separate but uses the same routing engine:
`POST /v1/chat/completions`, `GET /v1/models`, and `GET /v1/models/{model_id}`.
In v4.0, generation controls propagate when the selected provider supports
them; unsupported tools, response formats, and multimodal requests are rejected
instead of silently ignored.

## Audit Model

Committed consultations write three related records in one transaction:

- `graeae_consultations` — prompt, consensus, cost, latency, owner, namespace,
  and audit metadata columns.
- `graeae_audit_log` — hash-chain entry with `prev_hash` and `chain_hash`.
- `consultation_memory_refs` — memories injected or referenced by the
  consultation, when applicable.

If the audit write fails, consultation persistence fails too. This is the
current compliance boundary for GRAEAE. MNEMOS does not have one generic
`audit_log` table for every memory operation in v4.0; memory integrity is
tracked through the version DAG, webhook delivery rows, compression contest
records, and subsystem-specific audit tables.
