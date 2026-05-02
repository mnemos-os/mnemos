# KRONOS v0.1

KRONOS is the MNEMOS time-series layer for recall-pattern anomaly detection and
recall-load forecasting. v0.1 is CPU-only and uses the existing recall tracking
surface on `memories`: `recall_count` and `last_recalled_at`.

## Runtime Gate

KRONOS is disabled by default.

```bash
MNEMOS_KRONOS_ENABLED=true
MNEMOS_KRONOS_SENSITIVITY=2.5
MNEMOS_KRONOS_LOOKBACK_HOURS=168
MNEMOS_KRONOS_BASELINE_DAYS=30
```

Admin routes return `503` while the feature gate is off.

## Admin API

- `GET /admin/kronos/anomalies?namespace=<ns>` detects per-memory recall spikes
  and drops.
- `GET /admin/kronos/drift?namespace=<ns>` compares the last 7 days with the
  prior baseline window at namespace level.
- `GET /admin/kronos/forecast?namespace=<ns>&hours_ahead=24` forecasts recall
  load with an EWMA over hourly buckets and returns a 95% confidence interval.

All routes are root-only.

## MCP Tools

- `kronos_anomalies(namespace)`
- `kronos_forecast(namespace, hours_ahead=24)`

The MCP surface is read-only and uses the read rate-limit tier. Root callers may
inspect any namespace. Non-root callers only receive data for their own
namespace; cross-namespace anomaly requests return an empty result instead of a
distinguishable authorization error.

## v0.2 GPU Plan

v0.2 will integrate the Tesseract GPU path for large recall histories. The first
target is moving the EWMA bucket computation to a CUDA kernel while preserving
the v0.1 Python/NumPy results as the correctness oracle and CPU fallback.
