# MPF restore drill — dev ↔ prod import / export

CHARON's `mnemos/tools/memory_export.py` + `mnemos/tools/memory_import.py` are
the primary tools for moving an MNEMOS corpus between deployments.
This document is the operator runbook for the drill — exporting from
a source MNEMOS, validating the envelope, and restoring into a target.

## When to run this

- **Before a destructive migration** — schema change, host swap, version
  upgrade with risk.
- **Periodically as backup verification** — confirms exports are
  restorable, not just emittable.
- **For dev seeding** — clone a snapshot of prod into a staging instance
  to test against realistic data.
- **For instance migration** — move a corpus from one MNEMOS to another
  with different infrastructure (e.g. PYTHIA → PROTEUS).

## End-to-end procedure

The drill that lives below was validated on 2026-04-26 against
PYTHIA (v3.3.0, 11,769 memories) → PROTEUS staging (v3.4.0).
Throughput was ~770 records/sec end-to-end.

### 1. Export from source

```bash
TOKEN='d3a3bc609583005f4a077b6ffd00154b4f03f70104d0cdbfbb019fceb28daca9'
SOURCE='http://192.168.207.67:5002'

curl -s -H "Authorization: Bearer $TOKEN" \
    "$SOURCE/v1/export?include_sidecars=true&limit=10000" \
    -o /tmp/source-export.json
```

**Limits**:
- Default `limit` is 1000 records. Max accepted is 10000 — values above
  that return HTTP 422.
- For corpora > 10k records, paginate with `&offset=0`, `&offset=10000`,
  etc., or use the streaming JSONL export form (see CLI tool below).
- `include_sidecars=true` brings KG triples, memory-version DAGs, and
  compression-manifest sidecars into the envelope. Default is false to
  keep envelope size predictable.

### 2. Validate the envelope

```bash
cd /path/to/mnemos
python3 -m mnemos.tools.mpf_validate --file /tmp/source-export.json
```

Output should include `OK`. A failed validation means either the source
emitted a non-conformant envelope (file a bug on `mnemos-os/mnemos`) or
the file was corrupted in transit.

### 3. Import into target

**Direct POST to `/v1/import` works ONLY for envelopes ≤ 5 MB body.**
For anything bigger, use the CLI tool.

```bash
TARGET='http://192.168.207.25:5002'

python3 -m mnemos.tools.memory_import json \
    --file /tmp/source-export.json \
    --preserve-metadata \
    --endpoint "$TARGET" \
    --api-key "$TOKEN"
```

`--preserve-metadata` is the **dev↔prod restore flag** — it keeps record
ids, owner_ids, namespaces, and timestamps verbatim (no rewrite-on-
import). Without it, the importer treats the data as new memories
written by the calling user.

The CLI batches through `/v1/import` in groups of 200, so 21 MB
exports become 50 batched requests, each well under the 5 MB body cap.

### 4. Verify counts

```bash
curl -s "$TARGET/stats" | jq '{total: .total_memories, native: .native_memories, federated: .federated_memories}'
```

Native count should increase by the size of the imported corpus.
Federated count should be untouched.

### 5. Spot-check round-trip integrity

```bash
# Pick a known memory id from the source
SAMPLE_ID='mem_xxx'

# Fetch it from target
curl -s -H "Authorization: Bearer $TOKEN" \
    "$TARGET/v1/memories/$SAMPLE_ID" | jq '{id, content, owner_id, namespace, quality_rating}'
```

Compare the response to what the source had. Content, owner, namespace,
and quality should match byte-for-byte.

## Cleanup — removing test imports

When the drill was a test (not a real restore), you'll want to remove
the imported records to avoid bidirectional pollution (e.g. the target
later acts as a federation source for another node and re-emits the
test data as native).

**Schema gotcha**: `memory_versions` and `memory_branches` still lack a FK
cascade to `memories`. v5.0.0 includes the deletion-request workflow for
user-scope wipes, but restore-drill cleanup of test imports still needs
explicit DELETEs in dependency order, plus an orphan sweep:

```sql
BEGIN;

-- Drop branch HEAD pointers for native (test-imported) memories
DELETE FROM memory_branches
WHERE memory_id IN (SELECT id FROM memories WHERE federation_source IS NULL);

-- Drop version snapshots for native memories
DELETE FROM memory_versions
WHERE memory_id IN (SELECT id FROM memories WHERE federation_source IS NULL);

-- Drop the memories themselves (FK cascades clean compression_queue,
-- compressed_variants, candidates, quality_log)
DELETE FROM memories WHERE federation_source IS NULL;

COMMIT;
```

**Then** sweep orphaned `memory_versions` rows that the prior step
left behind (auto-created on import but no FK cascade on delete):

```sql
DELETE FROM memory_versions
WHERE memory_id NOT IN (SELECT id FROM memories);
```

After cleanup, verify:

```sql
SELECT 'native' AS kind, COUNT(*) FROM memories WHERE federation_source IS NULL
UNION ALL SELECT 'federated', COUNT(*) FROM memories WHERE federation_source IS NOT NULL
UNION ALL SELECT 'memory_versions', COUNT(*) FROM memory_versions
UNION ALL SELECT 'orphans',
    (SELECT COUNT(*) FROM memory_versions mv
     LEFT JOIN memories m ON m.id = mv.memory_id
     WHERE m.id IS NULL);
```

`native` and `orphans` should both be 0. `memory_versions` should equal
`federated` (one auto-version per memory).

## Cross-version compatibility

MPF v0.1.x is forward-compatible across MNEMOS minors. Validated
combinations as of 2026-04-26:

| Source | Target | MPF version | Result |
|---|---|---|---|
| MNEMOS v3.3.0 | MNEMOS v3.4.0 | 0.1.0 → 0.1.1 | ✅ clean round-trip |
| MNEMOS v3.4.0 | MNEMOS v3.4.0 | 0.1.1 ↔ 0.1.1 | ✅ clean round-trip |

Cross-major round-trips should be treated as release-drill work until validated
for the exact source and target versions; do not assume an unlisted 3.x, 4.x,
or 5.x pair has been exercised.

## Federation vs MPF — which to use when

| Use case | Right tool |
|---|---|
| Continuous replication between two MNEMOS instances | Federation (`/v1/federation/peers/{id}/sync`) |
| One-shot snapshot for backup or migration | MPF (`/v1/export` → `/v1/import`) |
| Moving memory between DIFFERENT systems (Mem0, Letta, etc.) | MPF, with the right adapter |
| Restoring after data loss | MPF from the latest backup |
| Seeding a dev instance from prod | MPF with `--preserve-metadata` |

Federation is the live wire, MPF is the file format. They share most
of the same shape on the records side; the difference is the transport.

## Troubleshooting

**HTTP 413 on `/v1/import`** — body is over 5 MB. Use the CLI
tool, which batches.

**HTTP 422 on `/v1/export`** — `limit` is over 10000. Drop to 10000
and paginate via `offset`, or use the streaming export.

**HTTP 503 on `/v1/federation/peers/{id}/sync`** — schema-compat
preflight failed transiently (network/timeout/5xx). Retry on the
next worker tick (60s); persistent 503 means the peer is genuinely
down.

**HTTP 409 on `/v1/federation/peers/{id}/sync`** — schema-compat
preflight detected a real version mismatch. Either update the peer or
flip its `compat_mode` to `permissive` (operator decision; see
`docs/FEDERATION.md` for the trade-off).

**Orphan `memory_versions` rows after delete** — known footgun until
[mnemos-os/mnemos#1](https://github.com/mnemos-os/mnemos/issues/1)
lands. Sweep with the orphan-cleanup query above.
