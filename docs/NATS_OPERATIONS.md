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

`_drain_partial(nc, subscriptions)` runs in BOTH consumer loops on:

1. Cancellation (`asyncio.CancelledError`)
2. Connect-level exceptions before `_consume_subscription` starts
3. Subscribe-level exceptions (durable name collision, stream
   drift, consumer-group recovery)
4. Receive-scope exceptions DURING consume (`next_msg` raising
   non-timeout NATS errors).
5. Ack-scope exceptions DURING consume (`_ack` failing — the broker
   is what we're acking to, so this is a NATS-connection issue).

Both (4) and (5) are re-raised out of `_consume_subscription` so they
reach the outer drain handler.

`_drain_partial` itself does:

1. Best-effort `await sub.unsubscribe()` for each successful
   subscription.
2. Best-effort `await nc.drain()` (falls back to `nc.close()`).

### Three-scope split inside `_consume_subscription`

`_consume_subscription` separates the per-message lifecycle into three
distinct try/except scopes — each with its own classification of
"escape for reconnect" vs "stay local":

| Scope        | Method            | Failure disposition                                                                |
|--------------|-------------------|------------------------------------------------------------------------------------|
| Receive      | `sub.next_msg`    | Timeout → continue. Anything else → re-raise (NATS issue, reconnect)                |
| Handle       | `handle_message`  | **Any** exception → log + don't ack + continue (subscription stays alive)           |
| Ack          | `_ack`            | Any exception → re-raise (broker-side issue, reconnect)                             |

The handle-scope is the load-bearing one for stability:

* `asyncpg.PostgresError` (transient DB hiccup)
* `asyncpg.InterfaceError` (closed/bad pool connection)
* `RuntimeError` from a custom store/fetch path
* HTTP errors from federation by-id backfill (401, timeout, etc.)

…all stay local. The NATS subscription itself is healthy in those
cases, and tearing it down would just delay unrelated traffic on the
same peer behind reconnect backoff. JetStream redelivers unacked
messages after the ack-wait window, so transient handler failures get
retried without code-side intervention.

Pre-round-2 (v4.2.0a6), a subscribe failure leaked one TCP connection
per retry. v4.2.0a7 round-2 added the receive/ack escape path for
genuine NATS issues. v4.2.0a7 round-3 (codex audit 2026-05-01) split
the handle scope from the receive/ack scopes so handler errors stay
local regardless of exception type — earlier code only kept
`asyncpg.PostgresError` local, which would have torn down NATS on a
plain `RuntimeError` or `asyncpg.InterfaceError`.

With backoff bounding the rate, drain bounding the total, the
receive/ack escape paths handling NATS issues, and the handle scope
keeping handler errors local, a sustained failure now stays in a
bounded steady state instead of accumulating sockets, wedging, or
amplifying handler hiccups into peer-wide reconnect storms.

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

## Multi-replica deployment (queue groups)

`v4.2.0a8` added JetStream queue-group support to both consumer
loops. By default the substrate is single-replica safe:

| Env var                                  | Effect when empty (default)                                                  | Effect when set                                                                                              |
|------------------------------------------|------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------|
| `MNEMOS_FEDERATION_NATS_QUEUE_GROUP`     | Per-(peer, subject) durable. Single-replica only.                            | Replicas joining this group share one durable per (peer, subject); JetStream load-balances messages.         |
| `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP`        | Per-node durable (every replica receives every nudge; SKIP LOCKED race).     | Shared durable named `mnemos_webhook_delivery_trigger`; JetStream delivers each nudge to ONE replica.        |

To roll out multi-replica:

1. Pick a stable group name (anything; `fed_pool` or `webhook_pool`
   are fine — it is just the JetStream `deliver_group` label).
2. Make sure EVERY replica that will join the group is on a build
   that understands the env var (v4.2.0a8 or later). A pre-a8
   replica on the same broker would collide on the durable name
   without the deliver_group set.
3. Set `MNEMOS_FEDERATION_NATS_QUEUE_GROUP` and/or
   `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP` on every replica and roll the
   fleet.
4. Operators MUST also set `MNEMOS_NODE_NAME` to a stable,
   per-replica unique value so `source_node` filtering still works
   for federation echo suppression.

If a partial fleet is on a8 and the rest on a7, run the older
replicas with the env vars unset; they will keep their
per-(peer,subject) or per-node durables and behave as before. The
queue-group durable on a8 replicas is independent.

When switching from per-node webhook durables to shared, the old
per-node durables remain on the broker until you delete them. They
will sit idle (no subscribers) until the 30-day age limit prunes
their inactive consumer state. To clean up immediately:

```
nats consumer rm MNEMOS_WEBHOOK mnemos_webhook_delivery_trigger_<old_node_name>
```

per old replica.

## Known limitations

* Broker-failure test depth — current tests cover happy-path
  publish/subscribe, the reconnect backoff scheduler, the three-
  scope `_consume_subscription` policy, and the queue-group
  subscribe shape, but do NOT exercise stream-drift scenarios
  (config mismatch on redeploy) or partial-broker-outage paths
  against a LIVE broker. Audit Finding 11 — open candidate for
  a future slice once a real-broker test harness is in place.
