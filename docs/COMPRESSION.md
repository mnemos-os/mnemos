# MNEMOS compression — operator-batched doctrine

Compression is opt-in and operator-batched through the admin endpoints:

- `POST /admin/compression/enqueue`
- `POST /admin/compression/enqueue-all`

Memory creation does not auto-enqueue contest jobs. This is deliberate:
compression is expensive work. It can involve LLM-judged contests and GPU
execution, and pervasive auto-compression on every create would saturate the
GPU pool while increasing cost-per-memory without an operator-controlled bound.

The compression flow is:

1. An operator enqueues specific memories or a bounded batch through the admin
   API.
2. Contest workers drain `memory_compression_queue`.
3. Eligible engines produce candidates and the judge selects winners.
4. `memory_compression_candidates` records the full contest audit trail.
5. `memory_compressed_variants` stores the current winning artifact.
6. `RehydrationResponse.compression_ratio` and
   `StatsResponse.average_compression_ratio` surface real ratios from those
   compression artifacts.

The admin endpoint request models and queue inserts live in
[`api/handlers/admin.py`](../api/handlers/admin.py).

Session messages and session memory injections do not carry compression tags.
Slice 12 dropped the always-NULL `compression_ratio` columns from
`session_messages` and `session_memory_injections`. Session memory injection
still slices `doc["content"][:500]` as a prompt-budget control. That is
truncation, not compression, and no ratio is recorded for it.

The Slice 12 migration drops are idempotent and safe: the removed session-layer
columns were always NULL fiction columns, not the real compression store.
