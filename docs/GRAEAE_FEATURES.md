# GRAEAE Feature Surface

**Status:** current v3.5.x documentation. v3.5.0 shipped on 2026-04-28;
v3.5.1 is the documentation-triage patch.

GRAEAE is MNEMOS's multi-provider reasoning bus. It fans a prompt out to live
LLM providers, scores the responses, persists consultations, and writes a
hash-chained audit record for each committed consultation.

## Runtime Modules

| Capability | Current module | Notes |
|---|---|---|
| Provider routing | `graeae/engine.py` | Config-driven providers with OpenAI-compatible, Anthropic, and Gemini adapter paths. |
| Circuit breaking | `graeae/_circuit_breaker.py` | Per-provider CLOSED / OPEN / HALF_OPEN guard; process-local. |
| Rate limiting | `graeae/_rate_limiter.py` | Sliding-window requests-per-minute guard; process-local. |
| Concurrency limiting | `graeae/_concurrency.py` | Per-provider async slot limiter; skips saturated providers instead of queueing them. |
| Quality weighting | `graeae/_quality.py` | Rolling success-rate and latency tracker used to adjust provider weights. |
| Response cache | `graeae/_cache.py` | In-memory normalized prompt cache with TTL; not a persistent semantic cache. |
| Model registry sync | `graeae/provider_sync.py`, `graeae/elo_sync.py`, `scripts/sync_provider_models.py` | Provider model discovery plus Arena.ai/LMArena weighting when the sync job is installed. |

The reliability state above is intentionally process-local in v3.5.x. MNEMOS
therefore remains pinned to one API worker for production correctness; moving
these guards to shared/external coordination is future horizontal-scaling work.

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

OpenAI-compatible access is separate but uses the same routing engine:
`POST /v1/chat/completions`, `GET /v1/models`, and `GET /v1/models/{model_id}`.
In v3.5.x, generation controls propagate when the selected provider supports
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
`audit_log` table for every memory operation in v3.5.x; memory integrity is
tracked through the version DAG, webhook delivery rows, compression contest
records, and subsystem-specific audit tables.
