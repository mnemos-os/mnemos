# NATS / JetStream Operations

Operational reference for the NATS substrate that backs MNEMOS v4.2's
federation push consumers and webhook delivery triggers. This is the
"what's actually on disk" companion to
[`mnemos/domain/graeae/MQ_INTEGRATION.md`](../mnemos/domain/graeae/MQ_INTEGRATION.md)
(GRAEAE consultation fan-out — separate, behind
`MNEMOS_GRAEAE_NATS_FANOUT` flag).

## Streams

`ensure_streams()` (in `mnemos/nats/client.py`) declares three streams
at startup. Declarations are idempotent — re-running against a broker
with a matching config is a no-op.

| Stream                | Subjects             | Retention      | Max bytes | Dedup window |
|-----------------------|----------------------|----------------|-----------|--------------|
| `MNEMOS_MEMORY`       | `mnemos.memory.>`    | 30 days, file  | 10 GB     | 2 minutes    |
| `MNEMOS_CONSULTATION` | `mnemos.consultation.>` | 30 days, file | 10 GB   | 2 minutes    |
| `MNEMOS_WEBHOOK`      | `mnemos.webhook.>`   | 30 days, file  | 10 GB     | 2 minutes    |

Storage is `FILE` (durable across broker restart). Retention is
`LIMITS` policy — messages drop when EITHER the 30-day age limit OR
the 10 GB byte limit is hit, whichever fires first.

The 2-minute `duplicate_window` matters for the publish-with-`msg_id`
pattern used by federation push and webhook nudges:
`nats_bus.publish_event(subject, payload, msg_id=<stable-id>)` will
not double-publish if the same `msg_id` arrives within 2 minutes.
Outside that window, a re-publish becomes a new message — consumers
must idempotency-check on the receive side (federation does this via
the memory `id` primary key + `ON CONFLICT`; webhook delivery does it
via the outbox `delivery_id` UUID).

### Storage exhaustion fallback

If the broker rejects `add_stream` with `insufficient storage
resources` (NATS error 10047), `ensure_streams()` retries with a
`max_bytes=1 GB` fallback so a small dev/test broker can still
declare the streams. Production should provision adequately to
avoid the fallback path.

### Replay window

Federation push consumers subscribe with `DeliverPolicy.NEW`
([`mnemos/federation/nats_consumer.py`](../mnemos/federation/nats_consumer.py)),
so a fresh process startup does NOT replay the entire 30-day
backlog. Operators who want to replay (e.g. after a peer was
offline) should use the HTTP federation pull path — it's the
canonical backfill route. The NATS consumer is an additive
fast-path; the HTTP poll is the durable fallback.

## Federation peer config

Set `MNEMOS_FEDERATION_NATS_PEERS` to a JSON array per peer:

```json
[
  {
    "name": "pythia",
    "nats_url": "nats://192.168.207.67:4222",
    "nats_token": "<NATS broker token>",
    "subjects": ["mnemos.memory.>"],
    "base_url": "http://192.168.207.67:5002",
    "auth_token": "<HTTP Bearer for /v1/federation/* endpoints>"
  }
]
```

| Field         | Required | Used for                                                     |
|---------------|----------|--------------------------------------------------------------|
| `name`        | yes      | Per-peer durable consumer name + log/metric label            |
| `nats_url`    | yes      | NATS connection target                                       |
| `nats_token`  | optional | Bearer-style token if the peer's broker requires auth        |
| `subjects`    | yes      | Subject patterns to subscribe to (typically `mnemos.memory.>`) |
| `base_url`    | yes      | HTTP federation endpoint for by-id backfill of replayed rows |
| `auth_token`  | yes      | HTTP Bearer for the peer's `/v1/federation/*` routes         |

Peers are loaded via `configured_nats_peers(settings)`. If the env
var is empty/unset, federation NATS is silently disabled (HTTP
federation pull continues). One consumer task is launched per peer
at startup — see `mnemos/api/lifecycle_hooks.py:_federation_nats_post_db_hook`.

## `MNEMOS_NODE_NAME`

Each NATS publish embeds `source_node = get_node_name()` so
consumers can filter loop-back (a peer's events that originated
from this node and were echoed back through federation).

If `MNEMOS_NODE_NAME` is unset, `get_node_name()` falls back to
`socket.gethostname()`. That works on a single host but **collides**
when multiple containers share the same hostname (common with
Docker Compose default container hostname=service name).

A boot-time warning fires when peers are configured but
`MNEMOS_NODE_NAME` is unset — see
`mnemos/api/lifecycle_hooks.py:_federation_nats_post_db_hook`.
Production deployments with federation peers should set this
explicitly to a stable, deployment-unique value.

## Reconnect backoff

`mnemos/nats/backoff.py:ReconnectBackoff` — exponential growth with
full jitter on broker outage. Both consumer loops use it:

* **Federation NATS consumer** —
  `mnemos/federation/nats_consumer.py:consumer_loop`
* **Webhook NATS trigger** —
  `mnemos/webhooks/nats_trigger.py:consumer_loop`

The window starts at 1s, doubles up to a 30s cap (overridable via
`retry_seconds` kwarg), and the actual sleep on each attempt is
`uniform(0, current_window)`. The window resets to base ONLY after
all subscriptions succeed, so a broker that accepts the connection
but rejects subscribe (stream drift, durable name mismatch) still
backs off rather than hot-looping.

### Why full jitter

Reference: AWS Architecture Blog,
"[Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)"
(Marc Brooker). With fixed-delay retry, a fleet of workers that
disconnected together also reconnects together — thundering herd.
Full jitter spreads the retry distribution uniformly across the
exponential window so collisions are rare.

## Resource cleanup on subscribe failure

`_drain_partial(nc, subscriptions)` runs in BOTH consumer loops on
ANY non-cancellation exception in connect/subscribe/consume:

1. Best-effort `await sub.unsubscribe()` for each successful
   subscription.
2. Best-effort `await nc.drain()` (falls back to `nc.close()`).

Pre-fix, a subscribe failure leaked one TCP connection per retry.
With backoff bounding the rate AND drain bounding the total, a
sustained subscribe failure now stays in a bounded steady state
instead of accumulating sockets.

## Operator runbook

### Symptom: federation events stop arriving from a peer

Check in this order:

1. `mnemos/federation/nats_consumer.py` log — look for
   "federation nats consumer peer=<name> unavailable: ...".
   The exception message identifies whether it's connect-level
   (broker down/unreachable) or subscribe-level (stream/consumer
   drift).
2. Broker reachability:
   `nats sub --server $PEER_NATS_URL "mnemos.memory.>"`
   (with `--auth-token` if the peer requires auth). If you see
   messages there but the consumer log is silent, the issue is
   in the consumer's subscribe path.
3. Stream presence on the peer:
   `nats stream info MNEMOS_MEMORY --server $PEER_NATS_URL`.
4. Durable consumer name collision:
   `nats consumer ls MNEMOS_MEMORY` — the federation consumer
   name pattern is `federation_<peer_name>_<sanitized_subject>`.
5. As a fallback, restart the local mnemos process — the HTTP
   federation pull path will still backfill any rows missed
   while NATS push was unavailable.

### Symptom: webhook deliveries delayed (broker outage)

* Pre-NATS: webhook delivery still happens via the polling
  recovery worker (`webhooks.repair_worker_loop` +
  `webhook_delivery_loop`) in `api/lifecycle_hooks.py`. Latency
  goes from ~real-time (NATS push trigger) to the polling cadence
  (`RECOVERY_POLL_INTERVAL`, default 30s).
* No deliveries are lost. The Postgres `webhook_deliveries`
  outbox is authoritative; NATS is a nudge fast-path only.

### Symptom: duplicate messages

Within a 2-minute window, the `duplicate_window` config blocks
re-publishes that supply the same `msg_id`. Outside that window
(network split lasting >2 min, broker restart spans the window),
duplicates can land. Consumers handle this via:

* Federation: memory `id` primary key + `ON CONFLICT (id) DO NOTHING`.
* Webhook: `webhook_deliveries.id` UUID primary key.

If you see duplicate side-effects despite the receive-side
idempotency, check whether a consumer is processing AT LEAST ONCE
(JetStream default) but treating the side effect as exactly-once.

### Symptom: stream config drift

`add_stream` is idempotent for MATCHING configs and raises for
mismatched configs. If you change `max_age` / `max_bytes` /
`duplicate_window` in `_stream_config()` and redeploy against a
running broker, the new declaration will fail with
"already in use" — the running stream keeps the OLD config. To
apply the new config, operator must `nats stream update` manually
or delete + recreate the stream (latter loses retained messages).

## Known limitations

* Federation NATS consumer uses **per-node durable** names (not a
  cross-node queue group). Every peer-side fan-out delivers to
  every receiving node, and each node's `SKIP LOCKED` claim
  decides who actually persists. Wasteful but correct. The
  cross-node sharding fix (`add_consumer` + `bind_subscribe`
  pattern) is the original handoff's "deferred-with-caveat"
  Audit Finding 5 — flagged for v4.2.0a7+. See the docstring of
  `mnemos/webhooks/nats_trigger.py:_node_durable` for the same
  pattern on the webhook side.
* Broker-failure test depth — current tests cover happy-path
  publish/subscribe and the reconnect backoff scheduler, but do
  NOT exercise stream-drift scenarios (config mismatch on
  redeploy) or partial-broker-outage paths against a live
  broker. Audit Finding 11 — v4.2.0a7+ candidate.
