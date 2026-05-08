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

## Payload sensitivity

What lands on each subject:

| Subject family               | Payload                                                                                                        | Sensitivity |
|------------------------------|----------------------------------------------------------------------------------------------------------------|-------------|
| `mnemos.memory.created.*`    | NUDGE only: `memory_id`, `namespace`, `category`, `source_node`. NO content body.                              | low-medium  |
| `mnemos.memory.updated.*`    | Same shape as `created` — id + namespace + category + source_node, no body.                                    | low-medium  |
| `mnemos.memory.deleted.*`    | `memory_id` + tombstone metadata.                                                                              | low         |
| `mnemos.consultation.*`      | Consultation `id`, `task_type`, model selection. Prompt/response excerpts NOT published — backends fetch via `/v1/consultations/{id}` for the body. | low-medium |
| `mnemos.webhook.*`           | Delivery `id`, `subscription_id`, `event_type`, target URL, payload hash. NOT the payload body.                | medium      |
| MCP SSE summaries (`/sse`)   | Filtered subset by default: `subject`, `memory_id`, `namespace`, `category`, `source_node`. Full content only when `MNEMOS_MCP_NATS_RAW=true`. | medium |

**Architectural note — why no full content on the bus.** The
shipped subjects are NUDGES, not content carriers. Federation push
receivers receive the nudge, then fetch the content via the
authorized HTTP federation feed (``GET
/v1/federation/feed?since=...&memory_id=...``) which enforces
per-peer ``namespace_filter`` / ``category_filter`` /
``auth_token`` — the same authorization predicate as the HTTP-pull
path. This means:

  * The broker does NOT hold a 30-day copy of every memory body.
    The streams retain 30 days of nudges (small JSON blobs); the
    body itself only lives in Postgres and travels over the
    authenticated HTTP feed when peers actually need it.
  * Per-peer authorization runs server-side at content fetch
    time, NOT at NATS subscribe time. A peer subscribed to
    ``mnemos.memory.created.*`` sees that an event happened (id +
    namespace + category) but cannot retrieve the body unless the
    HTTP feed authorizes the pull.
  * The metadata fields shipped on the bus (namespace, category,
    source_node) are themselves operator-classified data — a
    rogue subscriber learns WHAT topics are flowing, not WHAT was
    written. Rate-limit + ACL the bus accordingly (see next
    section), but don't treat broker storage as a content vault.

**Operator implication:** the broker's stream files are still
operationally important — they hold the activity audit trail and
nudge backlog. Encrypt at rest, restrict the filesystem, and
back up alongside Postgres. They're NOT, however, a parallel
content tier you have to encrypt with the same care as the
database itself, because no body is on them.

## NATS ACL recommendations

The MNEMOS publish/subscribe topology is asymmetric:

  * MNEMOS server processes PUBLISH on
    ``mnemos.memory.>``, ``mnemos.consultation.>``, ``mnemos.webhook.>``,
    ``mnemos.federation.>``.
  * MNEMOS server processes SUBSCRIBE to the same subjects (federation
    push receivers, webhook NATS triggers).
  * MCP HTTP/SSE clients SUBSCRIBE only to the principal-namespaced
    summary subset (subjects derived server-side from the
    authenticated principal — a non-operator client cannot pick its
    own subject filter; see ``mnemos/mcp/http.py::_parse_nats_sse_subjects``).

Recommended ``authorization`` block (NATS server config snippet):

```hocon
authorization {
  users = [
    # MNEMOS server processes — full pub/sub on every shipped subject.
    {
      user: "mnemos-server"
      password: "$MNEMOS_NATS_TOKEN"
      permissions: {
        publish:   { allow: ["mnemos.>"] }
        subscribe: { allow: ["mnemos.>", "_INBOX.>"] }
      }
    }

    # Operator / observability clients — subscribe-only.
    {
      user: "mnemos-observer"
      password: "$MNEMOS_OBS_TOKEN"
      permissions: {
        publish:   { deny: [">"] }
        subscribe: { allow: ["mnemos.>", "_INBOX.>"] }
      }
    }

    # External federation peers (if you trust a peer to publish into
    # YOUR memory namespace, which is unusual — most operators
    # prefer the HTTP federation feed for inbound). Scope tightly:
    # one user per peer with publish-only on a peer-prefixed
    # subject the local consumer subscribes to.
    # {
    #   user: "peer-alpha"
    #   permissions: {
    #     publish:   { allow: ["mnemos.federation.alpha.>"] }
    #     subscribe: { deny: [">"] }
    #   }
    # }
  ]
}
```

The "external federation peers" pattern is intentionally commented
out — most operators receive federation via the HTTP pull / push
endpoints, NOT by giving an external peer publish access to their
broker. Only enable it when the peer is operationally trusted at
the same level as the local mnemos process.

**Anti-pattern:** a single shared `mnemos` user with no per-role
split. A compromised MCP HTTP/SSE bridge would then have publish
authority on every memory subject, which lets an attacker forge
``mnemos.memory.created.<any-namespace>`` events that the
federation push receiver would write into the local store as
incoming federated rows. Always split publish from subscribe.

## Subject isolation per tenant

The MCP HTTP/SSE bridge derives subscriber subjects from the
authenticated principal:

  * Default subject for a non-operator client:
    ``mnemos.<event_class>.<event_action>.<safe_namespace>``
    where ``safe_namespace`` comes from the principal's
    ``user.namespace`` (sanitised to a NATS-safe token).
  * Operator-class principals (``role='root'``) MAY pass
    ``?subjects=mnemos.x.y.*`` to widen, but the substring filter
    must still start with ``mnemos.`` and contain no whitespace —
    enforced by ``_parse_nats_sse_subjects`` so a non-operator
    cannot tunnel arbitrary subject filters.

For multi-tenant deployments, the recommended hardening:

  1. Set ``MNEMOS_MCP_NATS_RAW=false`` (the default) so MCP SSE
     emits filtered summaries, not full content payloads.
  2. Scope NATS user permissions to per-namespace publish/subscribe
     prefixes when running multiple tenants on a shared broker.
     Example for tenant ``alice``:

     ```hocon
     {
       user: "tenant-alice"
       permissions: {
         publish:   { allow: ["mnemos.memory.created.alice",
                              "mnemos.memory.updated.alice",
                              "mnemos.memory.deleted.alice"] }
         subscribe: { allow: ["mnemos.>.alice", "_INBOX.>"] }
       }
     }
     ```

  3. Run separate broker accounts (NATS multi-tenancy primitive)
     for hard isolation between tenants who must NOT see each
     other's metadata even at the subject level.

## MCP event bridge — live-only contract

``GET /sse`` (the MCP HTTP/SSE event bridge in
``mnemos/mcp/http.py``) is a **live-only telemetry stream**, NOT
a replay-able audit log.

What that means in practice:

  * A SSE client connected at time T sees events published
    AFTER T. Events from before T are not surfaced; this
    bridge does not consume the JetStream stream's 30-day
    backlog.
  * The subscription uses ``DeliverPolicy.NEW`` +
    ``AckPolicy.NONE`` even when it goes through JetStream's
    consumer API, so messages are not acked back to the broker
    and the broker keeps no per-subscriber lag state.
  * If the underlying connection exposes the core-NATS handle
    (``js._nc``), the bridge uses core-NATS ``subscribe`` —
    even more clearly live-only with no JetStream involvement
    at all.

Why: SSE is a long-poll affordance for agent surfaces (Claude
Code, Cursor, ChatGPT Pro Developer Mode) that want to react
to NEW events as they fire. A replay-on-reconnect contract
would force operators to think about per-client cursors and
durable state, which is the opposite of what an interactive
agent loop wants — they generally just resubscribe and pick
up from "now."

Operators who need historical / audit-style reads:

  * Use the HTTP REST surface (``GET /v1/memories/...``,
    ``GET /v1/federation/feed``, ``GET /v1/memories/{id}/log``
    etc.) which goes through the visibility-gated repository
    path with the proper ``VisibilityFilter.for_read`` checks
    + RLS context.
  * The NATS streams (``MNEMOS_MEMORY``, ``MNEMOS_CONSULTATION``,
    ``MNEMOS_WEBHOOK``) DO retain 30 days of events for
    backend consumers — federation push receivers and webhook
    NATS triggers ARE durable. The MCP SSE bridge is the
    OUTLIER that gives up durability deliberately.

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
  `webhook_delivery_loop`) in `mnemos/api/lifecycle_hooks.py`. Latency
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

| Env var                              | Empty (default)                                                          | Non-empty                                                                                                                                                              |
|--------------------------------------|--------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `MNEMOS_FEDERATION_NATS_QUEUE_GROUP` | Durable: `mnemos_federation_<peer>_<subject>`. Single subscriber.        | Durable: `mnemos_federation_q_<group>_<peer>_<subject>` (queue == durable per nats-py). JetStream load-balances within the group.                                        |
| `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP`    | Durable: `mnemos_webhook_delivery_trigger_<node>`. Per-replica fan-out.  | Durable: `mnemos_webhook_delivery_trigger_q_<group>` (queue == durable per nats-py). JetStream delivers each nudge to exactly ONE replica.                               |

### Why durable == queue

`nats-py 2.14`'s `js.subscribe(subject, queue=Q, durable=D)` raises
``cannot create queue subscription 'Q' to consumer 'D'`` whenever
`D != Q` — internally the queue name *is* the durable name. mnemos
forces both to the same value (the full namespaced durable above)
and stamps the consumer's `deliver_group` to match. There is no
operator-visible knob to pull these apart.

### Rollout: separate-namespace coexistence (NOT a queue subscriber on a legacy durable)

Queue-mode durables intentionally live in a DIFFERENT namespace
(`_q_<group>_…`) from the legacy single-replica durables. This is
not cosmetic — it is the only way nats-py allows the two modes to
coexist on the same broker, because:

* nats-py rejects a queue subscription against a consumer whose
  `deliver_group` is unset (`cannot create a queue subscription
  for a consumer without a deliver group`).
* Switching an existing consumer's `deliver_group` requires
  delete-and-recreate; mnemos does not do this on your behalf.

So an a8 replica with queue-group set and an a7 replica running
default behavior land on **two separate JetStream consumers** for
the same stream. Both consumers receive every event published to
the stream. Both replica groups process those events. **Expect
the federation receive path to do duplicate work during a partial
upgrade window.** The persistence layer's
`ON CONFLICT (id) DO NOTHING` makes this idempotent at rest, but
the work itself is not free — get the partial-upgrade window
short, and prefer flipping the entire fleet at once when feasible.

The webhook side has the same shape, but the outbox `delivery_id`
UUID + Postgres `SKIP LOCKED` claim already serializes the actual
delivery work, so duplicate nudges land on the same outbox row and
only one wins. Less to worry about there.

### Steps

1. Pick a stable group name (e.g. `fed_pool`, `webhook_pool`).
2. Ensure every replica scheduled to join the group is on
   `v4.2.0a8` or later.
3. Set `MNEMOS_FEDERATION_NATS_QUEUE_GROUP` and/or
   `MNEMOS_WEBHOOK_NATS_QUEUE_GROUP` on those replicas.
4. Set `MNEMOS_NODE_NAME` per replica to a stable, unique value so
   `source_node` filtering still works (federation echo suppression
   does not depend on the queue group).
5. Roll the fleet. The queue-mode durable will be auto-created the
   first time a replica subscribes; subsequent replicas bind to it.
6. Once the fleet is fully on a8 + queue-group, you can delete the
   stale legacy durables to stop their event flow (and the
   duplicate work):

   ```
   nats consumer rm MNEMOS_MEMORY mnemos_federation_<peer>_<subject>
   nats consumer rm MNEMOS_WEBHOOK mnemos_webhook_delivery_trigger_<old_node_name>
   ```

   per legacy durable. JetStream auto-prunes inactive consumers
   after the 30-day age limit if you forget.

### Verifying queue-group rollout

After step 5, confirm the queue-mode consumer exists and has the
expected `deliver_group`:

```
nats consumer info MNEMOS_MEMORY \
  $(nats consumer ls MNEMOS_MEMORY | grep mnemos_federation_q_)
```

In the output, `Delivery Group:` should match the `_q_<group>_…`
prefix of the durable name. `Push Bound:` should be `true` while a
replica is subscribed.

To verify load-balancing across replicas (rather than one replica
taking everything), publish a small burst on the source side and
watch the per-replica receive logs:

```
# On the source node:
for i in $(seq 1 20); do
  nats pub mnemos.memory.created.default '{"memory_id":"verify_'"$i"'"}'
done

# Then on each receiver replica, count distinct verify_N IDs in
# the last minute of mnemos.federation.nats_consumer logs:
journalctl -u mnemos -n 1000 | grep "received=" | tail -5
```

A healthy queue group shows the burst spread across replicas (not
all 20 on one node). The exact split is not guaranteed even — NATS
delivers to whichever subscriber is currently free — but it should
not concentrate on a single replica when traffic is high enough.

If the burst lands entirely on one replica, check:

* All replicas actually have the env var set (`systemctl show
  mnemos -p Environment | grep QUEUE_GROUP`).
* All replicas are on `v4.2.0a8` or later
  (`mnemos --version`).
* The consumer's `Delivery Group` is set (queue-mode); if it's
  empty the consumer is in legacy single-subscriber mode and
  the env var didn't take effect.
* The replicas are in fact connected to the same broker — split-
  brain across two brokers means each broker has its own consumer
  with its own queue group.

## Live-broker integration tests

`v4.2.0a9` added `tests/integration_nats/` — a pytest suite that
runs against a real NATS broker. It is SKIPPED by default; set
`MNEMOS_NATS_TEST_URL=nats://host:4222` (and optionally
`MNEMOS_NATS_TEST_TOKEN`) to enable. The runtime contracts the
suite proves:

* `add_stream` is idempotent on a matching config.
* `add_stream` raises (does not silently mutate) on a drifted
  config; the existing stream keeps its old config.
* `ensure_streams()` is safe to re-run — second-call no-op.
* Queue-group subscriptions actually load-balance: two replicas
  joined to the same group both receive some traffic and JetStream
  does not duplicate messages across them.

Operators rolling out queue-group support can use this as a
pre-prod smoke check against their cluster:

```
MNEMOS_NATS_TEST_URL=nats://staging-broker:4222 \
  pytest tests/integration_nats/ -v
```

The suite creates per-test isolated streams (random suffix) and
deletes them in finalizers, so it is safe to point at a shared
broker — though running against a quiet staging cluster is
preferable.

## Known limitations

* Partial-broker-outage paths (broker shutdown mid-consume,
  durable consumer deletion mid-consume) are still only proved
  via the unit-level fakes in `tests/test_federation_nats_consumer.py`
  and `tests/test_webhook_nats_trigger.py`. A live-broker
  outage test would need a fixture that can stop+restart the
  broker subprocess; deferred candidate for v4.2.0a9+ once a
  test-managed broker fixture lands. The shipped
  ``MNEMOS_NATS_TEST_URL`` path expects the broker is operator-
  managed and stays up across the test session.
