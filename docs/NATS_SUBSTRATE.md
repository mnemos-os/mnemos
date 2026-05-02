# NATS Substrate

## v0.2 Boundary

NATS substrate v0.2 is a proof-of-life slice, not a broad queue migration.

When `MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING=1`, each PANTHEON routing-log write also publishes the same routing decision to `mnemos.pantheon.routing`. The event carries `metadata.schema_version` and is best-effort: publish failures are logged and never fail the gateway request. The existing MNEMOS `pantheon_routing` memory write remains in place.

When `MNEMOS_NATS_AUDIT_CONSUMER_ENABLED=1`, the optional `mnemos.workers.pantheon_routing_audit_consumer` worker subscribes to `mnemos.pantheon.routing` and mirrors events into `pantheon_routing_audit`. Apply `db/migrations_v4_2_pantheon_routing_audit.sql` before enabling the worker.

## v0.3 Deferred Work

Webhook outbox migration and federation rewire are intentionally deferred to v0.3. v0.2 proves one concrete producer/consumer pair on the existing substrate while keeping webhook durability and federation semantics unchanged.
