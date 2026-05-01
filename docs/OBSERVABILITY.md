# MNEMOS Observability — Operator Guide

Where to look when something is wrong, and how to set up Prometheus
+ Grafana so the answer is in front of you instead of buried in logs.

---

## 1. The four-instrument tier

MNEMOS ships four observability surfaces. They overlap in coverage
but each catches a different shape of problem:

| Instrument                   | Always-on? | Best at                                     |
|------------------------------|------------|---------------------------------------------|
| Structured logs              | yes        | Per-request narrative, error stack traces   |
| Prometheus metrics           | yes        | Rate / latency / saturation aggregates      |
| OpenTelemetry tracing        | opt-in     | Cross-service request spans                 |
| Audit hash chain             | yes        | Tamper-evident write history                |

This guide focuses on the metrics + Grafana setup. Logs are
covered in `OPERATIONS.md`; tracing lives in the `[tracing]`
extra; audit chain semantics are in `MEMORY_ARCHITECTURE.md` §4.2.

---

## 2. Scraping mnemos with Prometheus

MNEMOS exposes `/metrics` on the same port as the API by default.
A minimal `prometheus.yml` job:

```yaml
scrape_configs:
  - job_name: mnemos
    scrape_interval: 30s
    metrics_path: /metrics
    # By default /metrics is unauthenticated and carries no secrets,
    # only counters and histograms. Operators network-scope the
    # endpoint (private VPC, firewall rules, mesh ACLs).
    static_configs:
      - targets:
          - mnemos-prod-1:5002
          - mnemos-prod-2:5002
        labels:
          env: production
          tier: server
      - targets:
          - mnemos-edge-1:5102
        labels:
          env: production
          tier: edge
```

### 2.1 Optional: bearer auth on `/metrics`

If you can't network-scope (shared cloud Prometheus, public-internet-
routed clusters), set `MNEMOS_METRICS_REQUIRE_AUTH=true` on the
mnemos process. The endpoint then requires the same Bearer token
the rest of the API uses (looked up against the `api_keys` table —
revoked keys are rejected). Add the credential to Prometheus's
scrape config:

```yaml
scrape_configs:
  - job_name: mnemos
    scrape_interval: 30s
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials: <api-key-token>
    static_configs:
      - targets: [mnemos-prod-1:5002]
```

Default is `false` — flipping the env var does not change behaviour
for operators who already network-scope, and has zero startup cost
(the per-request check is one indexed `api_keys` lookup keyed on
the SHA-256 of the token).

If you run multiple replicas behind a load balancer, scrape each
backend directly (not the LB) so per-replica metrics distinguish
the replica that's hot from the one that's idle.

The default scrape interval (30s) is fine for the v4.x metric set.
Drop to 10s only if you're actively debugging a latency spike.

---

## 3. The shipped metrics

As of v4.2.0a11 the stable metric surface is:

### 3.1 HTTP request rate

```
mnemos_http_requests_total{method, route, status} (counter)
```

Labels:
- `method` — `GET`, `POST`, etc.
- `route` — FastAPI route template, e.g. `/v1/memories/{id}`. The
  template form prevents cardinality explosion across path
  parameters.
- `status` — `2xx` / `3xx` / `4xx` / `5xx` class. Per-code labels
  (`200`, `404`, `500`) were considered and rejected; we found
  the class is the right alerting granularity and full codes
  are visible in the per-request log line if needed.

### 3.2 HTTP request duration

```
mnemos_http_request_duration_seconds_bucket{method, route, le} (histogram)
mnemos_http_request_duration_seconds_count{method, route}
mnemos_http_request_duration_seconds_sum{method, route}
```

Histogram buckets are exponential, biased toward the sub-second
range mnemos actually serves. Use `histogram_quantile()` for p95/
p99 derivations:

```
histogram_quantile(0.99, sum by (le) (rate(mnemos_http_request_duration_seconds_bucket[5m])))
```

### 3.3 Process metrics (built-in)

`prometheus_client` registers these automatically:

- `process_resident_memory_bytes` — RSS
- `process_virtual_memory_bytes` — VSZ
- `process_open_fds` — open file descriptors
- `process_max_fds` — fd ulimit
- `process_cpu_seconds_total` — cumulative CPU time

A monotonically climbing `process_open_fds` is the canonical leak
signal — usually a socket not being closed in a federation peer
client or LLM-provider HTTP session.

### 3.4 Python runtime metrics (built-in)

`prometheus_client` also exports `python_gc_*`, `python_info`, etc.
Useful for diagnosing GC pauses but not normally watched.

---

## 4. The shipped Grafana dashboard

Import `docs/observability/grafana/mnemos-overview.json` via
Grafana → Dashboards → Import. The dashboard expects:

- A Prometheus data source named `prometheus` (the default in
  most installs).
- Scrape job named `mnemos` (matches the example above).

The dashboard has eight panels in three rows:

**Row 1 — request shape:**

1. **HTTP requests per second (by status class)** — overall
   throughput broken down by 2xx/3xx/4xx/5xx.
2. **Request duration p50/p95/p99** — latency percentiles.

**Row 2 — quality:**

3. **Error rate (5xx, %)** — 5xx as a percentage of total.
   Threshold: green <0.1%, yellow ≥0.1%, red ≥1%.
4. **Top 10 routes by request rate** — shows which endpoints
   are hot. Use this to target compression hot-path expansion
   and cache tuning.

**Row 3 — process:**

5. **Total requests (last 1h)** — single-stat counter.
6. **Average request duration (last 1h)** — single-stat with
   threshold (green <0.5s, yellow ≥0.5s, red ≥2s).
7. **Process RSS** — current resident memory.
8. **Process open FDs** — current file-descriptor count, with
   threshold (green <500, yellow ≥500, red ≥4000).

The dashboard variables `instance` and `route` let you scope to
a single replica or single endpoint when investigating an
incident.

---

## 5. Common patterns to watch

### 5.1 Spike in 5xx

5xx rates climbing above ~0.1% sustained means a downstream is
unhealthy. Check, in order:

1. **Database connectivity** — `mnemos serve` logs a "DB
   connection failed" line on every connection-pool acquire
   timeout. If the DB is the issue, all 5xx come back ~30ms
   into the request (the pool acquire timeout). Check for
   `asyncpg.PostgresError` in the log stream.
2. **LLM provider** — GRAEAE consultations 5xx when all
   configured providers fail. The 5xx will be concentrated on
   `/v1/consultations`. Check `mnemos.domain.graeae.engine` log
   for provider-specific failures; a single provider being down
   should NOT cause 5xx (we have fallback).
3. **Federation peer** — federation pull failures don't directly
   surface as 5xx (the worker handles them out-of-band) but
   federation-related routes (`/v1/federation/feed`) can if the
   local DB is unhealthy.

### 5.2 p99 spikes that don't track p50

When p99 climbs but p50 doesn't, the slow tail is concentrated
in a small fraction of requests:

- Federation by-id backfill (`/v1/federation/memory/{id}`) makes
  outbound HTTP to a peer; if the peer is slow, those specific
  requests block.
- LLM-provider consultations have multi-second baselines; any
  endpoint that triggers a synchronous consultation will show
  in the p99.
- Cold-start of compression engines (first call after process
  restart) can take 10s+ as ONNX models materialize.

If p99 sits at >5s sustained without p50 climbing, look at top-10
routes panel: probably a single hot endpoint with a slow downstream.

### 5.3 Climbing FD count

`process_open_fds` should be roughly stable in steady state
(maybe 100-300 open at peak). Monotonic climb suggests:

- A federation peer client not closing sessions properly. Check
  `mnemos.federation.nats_consumer` reconnect path; pre-v4.2.0a8
  there was a leak fixed in `_drain_partial`.
- An LLM provider client (httpx session) not closed. GRAEAE
  pools sessions; if pool size grows unbounded that's a leak.
- Postgres connection pool growing. Check pool config — should
  be bounded by `MNEMOS_DB_POOL_MAX_SIZE`.

The yellow/red thresholds (500/4000) are conservative for a
single replica. Adjust for your fleet size.

### 5.4 RSS climbing

A slow RSS climb (10 MB / hour over days) is usually a Python-side
cache that doesn't bound. The two known caches:

- `mnemos.api.lifecycle._cache` (Redis fallback in-memory cache).
  Bounded at startup; should not grow.
- `mnemos.domain.graeae.providers` — provider-specific session
  pools. Grow per-distinct-provider; bounded by your provider
  count.

A fast RSS climb (>100 MB / hour) is almost always a request
loop with a memory-resident result set — usually federation pull
loading too many rows in one cursor. Check `mnemos.domain.federation`
log for batch sizes.

---

## 6. Alerts to configure

Minimal alert set:

```yaml
groups:
  - name: mnemos
    rules:
      - alert: MnemosHigh5xxRate
        expr: |
          (sum(rate(mnemos_http_requests_total{status="5xx"}[5m]))
           / sum(rate(mnemos_http_requests_total[5m]))) > 0.01
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "MNEMOS 5xx rate >1% for 5min"
          description: "Sustained server errors. Check downstreams (DB, LLM, federation peers)."

      - alert: MnemosHighLatencyP99
        expr: |
          histogram_quantile(0.99,
            sum by (le) (rate(mnemos_http_request_duration_seconds_bucket[5m]))) > 5
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "MNEMOS p99 latency >5s for 10min"
          description: "Tail latency degraded. Check top-10 routes for the hot endpoint."

      - alert: MnemosFDLeak
        expr: |
          rate(process_open_fds{job="mnemos"}[1h]) > 0
          and process_open_fds{job="mnemos"} > 1000
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "MNEMOS file descriptor leak suspected"
          description: "Open FDs growing for 1h+ above 1000. Likely a session-not-closed bug."

      - alert: MnemosDown
        expr: up{job="mnemos"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "MNEMOS replica unreachable"
          description: "Prometheus failed to scrape the mnemos /metrics endpoint."
```

Tune the thresholds for your traffic shape. The defaults assume
production-tier traffic (hundreds of req/s); on a single-user
edge deployment you might never hit the 1% 5xx threshold even
during a real outage.

---

## 7. Future metric surface

The v4.x metric set is intentionally minimal. As features ship,
additional metrics will land:

- **Compression queue depth** — how many memories are pending
  compression. Federation hot-path expansion (v4.2.0a12+) will
  land this.
- **Federation pull lag** — wall-clock delta between a peer's
  newest memory and our last successful pull. Useful for
  diagnosing federation health from Grafana instead of via the
  CLI.
- **NATS consumer pending** — JetStream pending count per
  durable. Already visible via `nats consumer info`; metric
  would let Grafana alert on it.
- **MORPHEUS dream-state branch count** — how many active /
  archived / tombstoned branches per memory. Cardinality is
  per-memory which is high; will likely land as an aggregate.

All future additions will keep the existing metric names stable —
operators' alert configs won't need rewrites between minor
versions.

---

## 8. Cross-references

- `OPERATIONS.md` — broader operational runbook (logs, deploy,
  recovery).
- `NATS_OPERATIONS.md` — NATS substrate operator guide.
- `SCALING.md` — sizing + horizontal scale guidance.
- `docs/observability/grafana/mnemos-overview.json` — the
  Grafana dashboard import file.

---

*v1.0 — 2026-05-01. Tracks MNEMOS server v4.2.0a11.*
