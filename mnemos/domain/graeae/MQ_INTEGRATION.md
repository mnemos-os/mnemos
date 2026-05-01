# GRAEAE NATS Fan-Out Integration

Slice 6 preparation only. The engine remains on the existing in-process
provider calls until `MNEMOS_GRAEAE_NATS_FANOUT=true` is implemented by a
future slice.

> **Looking for the operational reference?** This doc covers the
> not-yet-flagged GRAEAE consultation fan-out design. For the live
> NATS substrate (federation push consumers + webhook delivery
> nudges, the three production streams MNEMOS_MEMORY /
> MNEMOS_CONSULTATION / MNEMOS_WEBHOOK, federation peer config,
> reconnect/backoff/cleanup behavior, and operator runbook) see
> [`docs/NATS_OPERATIONS.md`](../../../docs/NATS_OPERATIONS.md).

## Goal

Move GRAEAE consultation fan-out from ad-hoc in-process slot tracking to a
NATS-backed workflow shape that has explicit lifecycle state and cancellation
propagation. The intended outcome is structural: when the client disconnects or
the consultation task is cancelled, every in-flight muse call receives a cancel
message and the workflow is closed without relying on semaphore release paths.

## Feature Flag

`MNEMOS_GRAEAE_NATS_FANOUT` defaults to `false`.

While false, there is no behavior change:

- `GraeaeEngine.consult()` still performs provider eligibility checks,
  in-process fan-out, consensus computation, cache writes, and response shaping
  exactly as it does today.
- Existing `route()`, `route_stream()`, debate, majority, and single-provider
  paths remain unchanged.

When true in a future slice, only the consultation fan-out path should move
first. Gateway pass-through and streaming should stay on the current direct
provider calls until they get their own migration.

## Subject Layout

Consultation coordinator subjects:

- `mnemos.graeae.consult.start.<namespace>`: optional durable audit event for a
  new consultation workflow.
- `mnemos.graeae.consult.state.<namespace>`: JetStream workflow state updates.
- `mnemos.graeae.consult.cancel.<consultation_id>`: cancellation broadcast for
  every provider call in the workflow.
- `mnemos.graeae.consult.complete.<namespace>`: optional durable completion
  event once consensus is computed.

Provider request-reply subjects:

- `mnemos.graeae.provider.<provider>.request`: request-reply subject consumed by
  workers for one provider.
- `_INBOX...`: NATS-generated reply subject used by the coordinator.

Provider state subjects:

- `mnemos.graeae.provider.<provider>.started.<namespace>`
- `mnemos.graeae.provider.<provider>.completed.<namespace>`
- `mnemos.graeae.provider.<provider>.failed.<namespace>`
- `mnemos.graeae.provider.<provider>.cancelled.<namespace>`

Streams:

- `MNEMOS_GRAEAE_WORKFLOW`: `mnemos.graeae.consult.>` and
  `mnemos.graeae.provider.>` with file storage and a bounded retention window.
- The existing `MNEMOS_CONSULTATION` stream remains for public consultation
  events such as `mnemos.consultation.completed.<namespace>`.

## Payload Shapes

Coordinator to provider request:

```json
{
  "version": 1,
  "consultation_id": "uuid-or-route-id",
  "request_id": "uuid-for-this-provider-call",
  "namespace": "default",
  "source_node": "node-a",
  "provider": "openai",
  "prompt": "user prompt",
  "task_type": "reasoning",
  "timeout_seconds": 180,
  "model_override": "gpt-5.2-chat-latest",
  "generation_params": {},
  "request_params": {},
  "messages": null
}
```

Provider reply:

```json
{
  "version": 1,
  "consultation_id": "uuid-or-route-id",
  "request_id": "uuid-for-this-provider-call",
  "provider": "openai",
  "status": "success",
  "response_text": "...",
  "latency_ms": 1234,
  "model_id": "gpt-5.2-chat-latest",
  "choices": [],
  "error": null
}
```

Provider error reply:

```json
{
  "version": 1,
  "consultation_id": "uuid-or-route-id",
  "request_id": "uuid-for-this-provider-call",
  "provider": "openai",
  "status": "error",
  "response_text": "",
  "latency_ms": 0,
  "model_id": "gpt-5.2-chat-latest",
  "error": "RuntimeError: HTTP 500: ..."
}
```

Cancellation broadcast:

```json
{
  "version": 1,
  "consultation_id": "uuid-or-route-id",
  "request_ids": ["provider-request-uuid"],
  "namespace": "default",
  "source_node": "node-a",
  "reason": "client_disconnect"
}
```

Workflow state event:

```json
{
  "version": 1,
  "consultation_id": "uuid-or-route-id",
  "namespace": "default",
  "source_node": "node-a",
  "state": "running",
  "active_providers": ["openai", "claude"],
  "skipped_providers": ["groq"],
  "completed_providers": [],
  "cancelled": false,
  "updated_at": "2026-04-30T00:00:00Z"
}
```

## Cancellation Contract

The coordinator owns the consultation-level cancellation scope. For each
eligible provider, it sends a NATS request and records the generated
`request_id` in JetStream state before awaiting the reply.

If `consult()` receives `asyncio.CancelledError`, or a route detects a client
disconnect, the coordinator publishes
`mnemos.graeae.consult.cancel.<consultation_id>` with all active `request_ids`.
Provider workers subscribe to this subject while their HTTP call is in flight.
When a matching cancel arrives, the worker cancels the local provider task,
closes the HTTP stream/request, publishes a provider `cancelled` state event,
and suppresses the normal reply if the inbox is already gone.

The coordinator must treat timeout and cancellation differently:

- Request timeout: mark only that provider as error/unavailable and continue
  consensus with any other completed provider responses.
- Consultation cancellation: cancel every active request and re-raise
  `asyncio.CancelledError` so callers preserve current cancellation semantics.

## Migration Steps

1. Add `MNEMOS_GRAEAE_NATS_FANOUT` as a dark feature flag. Keep it default false.
2. Add `MNEMOS_GRAEAE_WORKFLOW` stream declaration for
   `mnemos.graeae.consult.>` and `mnemos.graeae.provider.>`.
3. Extract the existing `_query_provider()` call into a provider-worker handler
   that can execute one request payload and return the existing provider
   response dict shape.
4. Migrate one low-risk provider first, preferably an OpenAI-compatible provider
   such as `groq` or `together`, behind the flag.
5. Add coordinator fan-out for flagged providers only; unflagged providers keep
   the direct in-process call so mixed mode is possible.
6. Add cancellation tests with a fake NATS responder: cancel `consult()`, assert
   the cancel subject receives the active `request_id`, and assert the provider
   worker cancels the pending HTTP task.
7. Migrate the remaining OpenAI-compatible providers one provider per slice.
8. Migrate Anthropic and Gemini after adapter-specific payload parity tests are
   in place.
9. Remove consultation semaphore acquisition from the NATS fan-out path only
   after all providers used by `consult()` have responder coverage.
10. Flip the flag in staging, compare response shape and consensus fields
    against direct mode, then make NATS fan-out the default in a later release.
