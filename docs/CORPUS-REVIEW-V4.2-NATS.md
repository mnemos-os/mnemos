# CORPUS-REVIEW-V4.2-NATS

## Executive Summary

Reviewed the v4.2 NATS slice at HEAD `e4c54d3` against baseline `50166c8`. The new HTTP routes inspected still use `get_current_user`, and I did not find API keys, bearer tokens, or webhook HMAC secrets published into NATS payloads. The main regressions are in the bus security model: full memory content and webhook endpoint URLs are published to broad subjects, while the MCP NATS SSE bridge and federation consumer can subscribe without applying the existing per-user/per-namespace visibility checks. I found 2 critical, 5 high, 3 medium, and 2 low findings.

## Top Findings

| # | Severity | Location | Summary |
|---:|---|---|---|
| 1 | critical | `mnemos/mcp/http.py:352`, `mnemos/mcp/http.py:453`, `mnemos/api/routes/memories.py:724` | MCP NATS SSE exposes full bus payloads across namespaces. |
| 2 | critical | `mnemos/federation/nats_consumer.py:26`, `mnemos/api/routes/memories.py:724`, `mnemos/domain/federation.py:624` | NATS federation receives full memory content with no feed-equivalent authorization/filtering. |
| 3 | high | `mnemos/api/routes/memories.py:226`, `mnemos/api/routes/ingest.py:49` | Ingest publishes memory content before the enclosing DB transaction commits. |
| 4 | high | `mnemos/federation/nats_consumer.py:276`, `mnemos/webhooks/nats_trigger.py:103` | Consumers ack messages even after processing errors. |
| 5 | high | `mnemos/webhooks/outbox.py:73`, `mnemos/webhooks/nats_trigger.py:74`, `mnemos/webhooks/nats_trigger.py:121` | Webhook NATS trigger amplifies every delivery across every node. |
| 6 | high | `mnemos/mcp/http.py:416`, `mnemos/mcp/http.py:431` | NATS SSE uses an unbounded per-client queue. |
| 7 | high | `mnemos/nats/client.py:63`, `mnemos/nats/client.py:67` | Failed startup stream declarations still leave publishing enabled. |
| 8 | medium | `mnemos/federation/nats_consumer.py:116`, `mnemos/webhooks/nats_trigger.py:45` | Reconnect loops use fixed 30s retry with no backoff/jitter. |
| 9 | medium | `mnemos/nats/client.py:34`, `mnemos/core/config.py:382` | `MNEMOS_NODE_NAME` silently falls back to hostname. |
| 10 | medium | `mnemos/mcp/http.py:382` | MCP event bridge uses core NATS subscriptions, bypassing JetStream stream/retention semantics. |
| 11 | low | `mnemos/domain/graeae/MQ_INTEGRATION.md:171`, `mnemos/core/config.py:377` | MQ integration docs omit shipped streams and operational security guidance. |
| 12 | low | `tests/test_nats_publisher.py:22`, `tests/test_mcp_nats_sse.py:72` | Test coverage is mostly mock-level and does not exercise real broker failure modes. |

## Findings Detail

### 1. MCP NATS SSE exposes full bus payloads across namespaces

Severity: critical  
Location: `mnemos/mcp/http.py:352`, `mnemos/mcp/http.py:453`, `mnemos/api/routes/memories.py:724`, `mnemos/api/routes/webhooks.py:115`  
Blast-radius: cross-tenant data leakage through authenticated MCP HTTP/SSE clients

`/mcp/events/stream` is protected by bearer auth, but it is not bound to the principal's namespace. With no query string it subscribes to `mnemos.>`, and caller-supplied filters only need to start with `mnemos.`. Memory create events include full `content`, `owner_id`, and `namespace`; webhook subscription events include the target URL. An MCP token for Alice can therefore stream Bob's bus events if both share the same MCP edge and NATS connection.

Suggested fix: make the event stream opt-in per principal and derive subjects server-side from the authenticated principal, e.g. `mnemos.memory.*.<safe_user_namespace>`. Do not accept arbitrary subject filters from clients unless the caller is root/operator. Consider publishing redacted event summaries and requiring normal REST/MCP tool reads for content, where visibility predicates already run.

### 2. NATS federation receives full memory content without feed-equivalent authorization

Severity: critical  
Location: `mnemos/federation/nats_consumer.py:26`, `mnemos/federation/nats_consumer.py:162`, `mnemos/api/routes/memories.py:724`, `mnemos/domain/federation.py:624`  
Blast-radius: cross-namespace leakage to federation peers

The default federation NATS subjects expand to all memory create/update/delete subjects. Create events carry full memory content, and the push consumer writes whatever arrives through `_store_memories`. The HTTP federation pull path has an authenticated feed with namespace/category filters; the NATS path has only operator-configured subjects and no per-peer authorization check comparable to the feed endpoint. `_store_memories` caps field sizes and uses a federated owner, but it does not decide whether that peer should have received the memory.

Suggested fix: do not publish private memory content to broad subjects. Either publish IDs/metadata only and let peers fetch through the existing federation feed, or add per-peer allowlists and signed/encrypted subjects that are generated from the same namespace/category policy as HTTP federation.

### 3. Ingest publishes memory content before transaction commit

Severity: high  
Location: `mnemos/api/routes/memories.py:221`, `mnemos/api/routes/ingest.py:49`, `mnemos/api/routes/ingest.py:75`  
Blast-radius: correctness and confidentiality for session ingestion

`_insert_memory_with_created_webhook()` publishes the NATS `memory.created` event inside the caller's transaction. `ingest_session()` loops over multiple inserts inside one transaction; if a later insert fails and the transaction rolls back, earlier full-content NATS events have already left the process for rows that never committed. That creates ghost events and can leak session content from failed writes.

Suggested fix: return post-commit NATS event intents from the helper and publish them after the transaction commits, matching the outbox scheduling pattern used by the main memory route.

### 4. Consumers ack after processing errors

Severity: high  
Location: `mnemos/federation/nats_consumer.py:272`, `mnemos/federation/nats_consumer.py:276`, `mnemos/webhooks/nats_trigger.py:99`, `mnemos/webhooks/nats_trigger.py:103`  
Blast-radius: at-least-once reliability

Both consumers ack in `finally` whenever a message was received. If JSON decoding, DB acquisition, `_store_memories`, delete handling, or scheduling raises, the message is still acknowledged and JetStream will not redeliver it. For federation deletes especially, the HTTP pull backfill path may not recover the missed event.

Suggested fix: ack only after successful, idempotent handling. Use `nak` or leave the message unacked on transient failures, and explicitly ack malformed poison messages after logging/metrics.

### 5. Webhook NATS trigger amplifies every delivery across every node

Severity: high  
Location: `mnemos/webhooks/outbox.py:73`, `mnemos/webhooks/chain.py:62`, `mnemos/webhooks/nats_trigger.py:74`, `mnemos/webhooks/nats_trigger.py:121`  
Blast-radius: broker, DB, and task pressure under webhook load

Every outbox insert and retry successor publishes `mnemos.webhook.delivery.queued.*`. The trigger creates a per-node durable, so every node receives every delivery nudge and schedules a task; DB leases ensure only one send wins, but all nodes still race to claim. The original route also schedules local delivery attempts after commit, so a single user write with several subscriptions can produce many extra tasks and DB claims.

Suggested fix: use a queue group/shared durable for webhook nudges, or only publish nudges for rows not already scheduled locally. Add metrics and load tests for `subscriptions x nodes x retries`.

### 6. NATS SSE uses an unbounded per-client queue

Severity: high  
Location: `mnemos/mcp/http.py:416`, `mnemos/mcp/http.py:421`, `mnemos/mcp/http.py:442`  
Blast-radius: process memory exhaustion

Each SSE client gets `asyncio.Queue()` with no max size. A slow or stalled client subscribed to `mnemos.>` can accumulate every bus message in memory. Because payloads can include full memory content, one client can become a memory sink during bulk imports or webhook storms.

Suggested fix: set a bounded queue, drop or coalesce events when full, and close slow streams with a clear SSE error. Restrict default subjects to low-volume summaries.

### 7. Failed stream declarations still leave publishing enabled

Severity: high  
Location: `mnemos/nats/client.py:66`, `mnemos/nats/client.py:67`, `mnemos/nats/client.py:126`  
Blast-radius: silent loss of bus events

`connect_nats()` calls `ensure_streams(js)` and then stores `_jetstream` even when stream creation logs warnings for non-idempotent failures. Publishers then report publish failures per event, but startup appears "JetStream context ready". A missing/misconfigured stream can silently disable all slice behavior while the process otherwise looks healthy.

Suggested fix: make required stream declaration failures fatal to NATS enablement. Return `None` unless all required streams exist with compatible subjects/limits, and expose readiness/metrics for NATS state.

### 8. Reconnect loops use fixed retry without backoff

Severity: medium  
Location: `mnemos/federation/nats_consumer.py:116`, `mnemos/federation/nats_consumer.py:123`, `mnemos/webhooks/nats_trigger.py:45`, `mnemos/webhooks/nats_trigger.py:51`  
Blast-radius: broker outage behavior

Both consumer loops retry every 30 seconds forever with no exponential backoff or jitter. With many processes or peers, a broker outage or DNS issue causes synchronized reconnect waves.

Suggested fix: use capped exponential backoff with jitter and reset the backoff after a sustained healthy connection.

### 9. Node name silently falls back to hostname

Severity: medium  
Location: `mnemos/nats/client.py:34`, `mnemos/nats/client.py:39`, `mnemos/core/config.py:382`  
Blast-radius: loop prevention and durable naming

If `MNEMOS_NODE_NAME` is unset, the code mutates settings to `socket.gethostname()` without warning. Container hostnames can collide, change on restart, or differ across blue/green deploys, which affects `source_node` self-loop checks and webhook durable names.

Suggested fix: log a warning when falling back, document uniqueness requirements, and consider requiring explicit node names whenever NATS consumers are enabled.

### 10. MCP event bridge bypasses JetStream durability semantics

Severity: medium  
Location: `mnemos/mcp/http.py:382`, `mnemos/mcp/http.py:386`, `mnemos/mcp/http.py:397`  
Blast-radius: correctness/operator expectations

When the underlying NATS connection is reachable via `js._nc`, the SSE bridge uses core NATS `subscribe()` instead of JetStream. That means no stream binding, deliver policy, or replay semantics, while the route is advertised as NATS-backed SSE over MNEMOS events. Operators may expect retained JetStream behavior and get live-only core subscriptions.

Suggested fix: use explicit JetStream subscriptions for the bridge, or document that this endpoint is live-only telemetry.

### 11. MQ integration docs omit shipped NATS slice behavior

Severity: low  
Location: `mnemos/domain/graeae/MQ_INTEGRATION.md:1`, `mnemos/domain/graeae/MQ_INTEGRATION.md:171`  
Blast-radius: operator confusion

The only MQ integration document is scoped to future GRAEAE fan-out. It does not document the shipped memory, consultation, webhook, federation, MCP SSE subjects, retention, node naming, auth model, or subject isolation requirements.

Suggested fix: add a v4.2 NATS operations section covering subjects, payload sensitivity, ACL recommendations, node naming, stream sizing, and failure modes.

### 12. NATS tests do not exercise real broker failure modes

Severity: low  
Location: `tests/test_nats_publisher.py:22`, `tests/test_federation_nats_consumer.py:176`, `tests/test_webhook_nats_trigger.py:107`, `tests/test_mcp_nats_sse.py:72`  
Blast-radius: regression detection

The slice has useful wiring tests, but most use fake JetStream/subscription objects. Missing coverage includes real JetStream ack/redelivery behavior, stream declaration mismatch, broker disconnect/reconnect, slow SSE clients, multi-node webhook trigger amplification, and namespace isolation for MCP event streams.

Suggested fix: add an optional integration test tier using an ephemeral NATS container plus focused unit tests for ack-on-error, bounded SSE queues, and principal-to-subject enforcement.

## Things That Are Right

- The inspected FastAPI route additions in memories, consultations, and webhooks retain `get_current_user`.
- NATS payloads do not include API keys, NATS tokens, bearer tokens, or webhook HMAC secrets.
- The webhook trigger does not trust the NATS payload URL for sending; it passes only `delivery_id` into the existing sender, which reloads the DB row and uses the pinned DNS validation path.
- Federation push uses stable local IDs through `_store_memories`, so create/update redelivery is mostly idempotent.
- The GRAEAE provider-worker extraction preserves the local direct-call path while keeping the NATS fan-out flag dark.
