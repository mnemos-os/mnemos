# Search Latency Notes

Context: live PYTHIA production `POST /v1/memories/search` latency measured on
2026-05-04 was p50=1527ms, p95=1913ms, p99=1931ms, mean=1579ms,
stdev=144ms over a corpus of about 7,500 memories. Image v5.0.7 had
`mnemos_hot` enabled; the isolated Rust rerank path measured about 66ms.

## Code Path Read

Handler: `mnemos/api/routes/memories.py::search_memories`.

Semantic search flow:

1. FastAPI parses `MemorySearchRequest` before handler entry.
2. Handler computes `request_limit = min(request.limit, 500)` and a Redis cache
   key. A cache hit returns before embedding or Postgres.
3. `mnemos.core.lifecycle._get_embedding(request.query)` truncates the query to
   2000 characters, creates a new `httpx.AsyncClient`, and calls
   `{INFERENCE_EMBED_HOST}/v1/embeddings` with `INFERENCE_EMBED_MODEL`.
   It falls back to `{INFERENCE_EMBED_HOST}/api/embeddings` only on HTTP 404.
4. `PostgresMemoryRepository.semantic_search` builds one pgvector SQL query
   over `memories`, selecting full memory columns plus similarity and ordering by
   `embedding <=> $1::vector`.
5. Optional recency rerank is implemented in `semantic_search` when
   `boost_recency=True` and more than one row returns. It widens candidates to
   `max(limit, min(limit * 4, 200))`, selects `embedding::text`, computes a
   recency boost in SQL, parses returned vectors, then calls `_rerank_composite`.
   `_rerank_composite` uses `mnemos_hot.rerank_composite` when
   `MNEMOS_HOT_RS_ENABLED=1` and the wheel imports, otherwise Python fallback.
6. `row_to_memory` builds Pydantic `MemoryItem` objects. It parses `metadata`
   only if the DB returned it as a JSON string.
7. `MemoryListResponse` is returned. FastAPI performs final response encoding
   after handler return. The handler also calls `response.model_dump_json()` for
   the 5 minute Redis search cache write when cache is enabled.

No LLM judge prompt is on this request path. The only remote model call in the
search path is the embedding request.

## Phase Estimates

These estimates are hypotheses from code reading plus the 1527ms p50. The new
timing logs should replace them with measured per-request deltas.

| Phase | Code boundary | Estimate | Notes |
| --- | --- | ---: | --- |
| parse | handler entry and cache key setup | <5ms | FastAPI body parsing occurs before handler entry. Redis cache read is outside this estimate and can short-circuit the path. |
| embed | `_get_embedding` HTTP call | 600-1200ms hypothesis | Most likely hot path if the embedding server is remote, cold, saturated, or `/v1/embeddings` returns 404 and forces the `/api/embeddings` fallback. |
| ann_scan | `conn.fetch` in `semantic_search` | 100-700ms hypothesis | One pgvector query fetches full memory rows. Cost depends on index use, filters, visibility predicates, pool wait, and result row size. |
| rerank | `_rerank_composite` block | 0ms on current route unless recency rerank is enabled; 66ms isolated measurement | The repository supports rerank, but as read here the successful semantic handler call does not pass `request.boost_recency` into `semantic_search`. |
| metadata_fetch | after DB transaction | ~0-20ms | No N+1 metadata fetch exists in the Postgres path. Metadata is folded into the ANN/FTS SELECT via `_MEMORY_COLS`. |
| serialize | response model construction and cache JSON | 5-50ms hypothesis | Scales with `limit`, content size, metadata size, and cache JSON encoding. Final FastAPI response encoding occurs after handler return. |

## Index And K

`db/migrations.sql` creates:

```sql
CREATE INDEX IF NOT EXISTS idx_memories_embedding
ON memories USING ivfflat(embedding vector_cosine_ops);
```

No code path found setting `ivfflat.probes`, `hnsw.ef_search`, or another
pgvector scan parameter for this endpoint. No HNSW index definition was found in
the read path. Query K is the client `request.limit`, capped in the handler at
500. If recency rerank is active in the repository, candidate K widens up to
`min(limit * 4, 200)`.

## Likely Hot Path

Primary hypothesis: embedding latency dominates. `_get_embedding` creates a new
HTTP client per request and performs a remote embedding call with a default
`INFERENCE_EMBED_TIMEOUT` of 10 seconds. If the configured host does not serve
OpenAI-compatible `/v1/embeddings`, every request pays for one failed POST before
the Ollama-compatible fallback.

Secondary hypothesis: ANN scan dominates if embedding timing is low. The query
selects full memory rows, including `compressed_content`, then orders by
`embedding <=> $1::vector` with visibility predicates. Confirm with the new
`ann_scan` timing plus `EXPLAIN (ANALYZE, BUFFERS)` for the exact generated SQL.

Rerank is not the dominant source if the isolated 66ms measurement holds.

## Tuning Options Without Rebuilding

- Point `INFERENCE_EMBED_HOST` directly at the fastest endpoint shape that
  returns 200 for `/v1/embeddings` to avoid the 404 fallback. Related env vars:
  `INFERENCE_EMBED_HOST`, `INFERENCE_EMBED_MODEL`, `INFERENCE_EMBED_TIMEOUT`.
- Lower client `limit` for broad searches. The current only server guard is the
  hardcoded `min(request.limit, 500)` in `search_memories`; there is no config key
  for a lower global max.
- Keep `boost_recency=false` when rerank is not required. Request controls:
  `boost_recency`, `recency_weight`. Repository rerank widens candidates and can
  add vector parsing plus Rust/Python rerank cost when wired.
- Tune pgvector probes at the session/database level if ANN scan is high.
  Current code has no `ivfflat.probes` config. A future config would fit near
  `PostgresMemoryRepository.semantic_search` before `conn.fetch`. `hnsw.ef_search`
  is not relevant unless the index is changed to HNSW.
- Check database pool pressure if `ann_scan` includes connection wait. Existing
  controls: `PG_POOL_MIN`, `PG_POOL_MAX`, and `MNEMOS_POOL_ACQUIRE_TIMEOUT`.
- Use the existing 5 minute Redis response cache for repeated identical searches.
  The cache key includes user, namespace, filters, group IDs, semantic/FTS mode,
  archive flag, and recency fields. TTL is hardcoded as 300 seconds in the
  handler; no config key exists.

## Where To Add Missing Controls

- Embedding cache: add around `_get_embedding` or immediately before the handler
  call, keyed by normalized query hash plus `INFERENCE_EMBED_MODEL` and host.
  No existing embedding-cache env var was found.
- Search max K: add a settings key in `mnemos/core/config.py` and use it instead
  of the hardcoded 500 cap in `search_memories`.
- pgvector probes: add a settings key in `mnemos/core/config.py`, then apply
  `SET LOCAL ivfflat.probes = ...` inside the Postgres transaction before the
  ANN `conn.fetch`.
- Search cache TTL: replace the hardcoded `300` in `search_memories` with a
  runtime setting if operators need to tune repeat-query behavior.

## Observability Added In v5.0.10

Each search request now gets a short `trace_id` and logs elapsed milliseconds
since handler start at these boundaries:

- `embed`
- `ann_scan`
- `rerank`
- `metadata_fetch`
- `serialize`

Example:

```text
[search:abc123] embed done in 823ms
[search:abc123] ann_scan done in 1235ms
[search:abc123] rerank done in 1301ms
[search:abc123] metadata_fetch done in 1302ms
[search:abc123] serialize done in 1314ms
```

Subtract adjacent timestamps with the same trace id to get phase deltas.
