# CHARON Peer Adapters

Pure-Python converters between MPF v0.1.x envelopes and the native
record shapes of five other agent-memory systems. **No runtime
dependency on the peer SDKs** — these are shape-converters that work
on plain dicts, so they can ship in the MNEMOS tree without dragging
in mem0 / letta / graphiti / cognee / mempalace as install-time deps.

The full bidirectional test rig (which does install each peer in its
own venv and runs round-trips end-to-end) lives outside this repo.
See the announcement matrix for results.

## Contract

Each adapter exposes two functions:

```python
def from_mpf(envelope: dict) -> dict[str, list[dict]]:
    """Convert an MPF envelope into native records grouped by kind.
    
    Returns e.g. {'memories': [...], 'kg_triples': [...]} where the
    inner dicts are shaped per the peer's schema. Sidecar arrays
    are converted to whatever the peer's KG / version / compression
    surface looks like (or omitted when the peer has no equivalent).
    """

def to_mpf(records: dict[str, list[dict]]) -> dict:
    """Convert native peer records into an MPF v0.1.1 envelope.
    
    The inverse of from_mpf. Lossy fields (peer-specific extensions
    that don't fit MPF) go into the per-record `metadata` object
    under a producer-namespaced key (e.g. `letta_archive_id`).
    """
```

## Five adapters

| System | Adapter file | Native record | Sidecar fit |
|---|---|---|---|
| Mem0 | `mem0.py` | `Memory` (id + memory + user_id + agent_id + metadata + categories) | kg_triples ↔ Graph Memory mode; memory_versions ↔ history() endpoint |
| Letta | `letta.py` | `Passage` (id + text + archive_id + organization_id + tags) | None native — passages have no version model. Map archive_id ↔ namespace |
| Graphiti | `graphiti.py` | `EpisodicNode` + `EntityNode` + `EntityEdge` (graph-native) | kg_triples ↔ EntityEdge with valid_at/valid_until match 1:1 |
| Cognee | `cognee.py` | `DataPoint` subclass + extracted graph | kg_triples ↔ post-cognify graph nodes/edges |
| MemPalace | `mempalace.py` | drawer entry (text + wing + room + drawer) + diary | kg_triples ↔ temporal entity-relationship graph (SQLite-backed) |

## Field-loss policy

Adapters preserve everything they can; everything they can't goes to
metadata under a namespaced key. Field-loss is documented per-adapter
in the docstring (which fields survive, which go to metadata, which
are dropped because the peer has no equivalent slot).

The CHARON design assumes `additionalProperties: true` everywhere in
MPF, so a producer-side field that doesn't fit one peer can still
round-trip through MNEMOS unchanged.

## Status

Adapter scaffolding only — wire shapes documented, code coming after
the CHARON branch lands. Pin: see commit `feat(charon): v0.2 — full
MPF sidecar round-trip` for the schema this targets.
