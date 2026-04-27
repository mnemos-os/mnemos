#!/usr/bin/env python3
"""
tools/adapters/graphiti.py — CHARON adapter for Graphiti temporal KGs.

Converts a Graphiti temporal knowledge graph into an MPF v0.1
envelope. Reads the backing graph store directly (Neo4j by default;
FalkorDB and Kuzu also supported when the corresponding driver is
installed) with Cypher — no Graphiti runtime required on the
exporter side. This matches the pattern used by mempalace.py:
adapters must be operable against a quiesced snapshot even when
the source project's Python runtime is unavailable.

Usage:
    # Default Neo4j bolt endpoint + default auth env vars:
    python -m tools.adapters.graphiti \\
        --neo4j bolt://localhost:7687 \\
        --neo4j-user neo4j --neo4j-password $NEO4J_PASSWORD \\
        --out graph.mpf.json

    # FalkorDB (RedisGraph fork):
    python -m tools.adapters.graphiti \\
        --backend falkordb --falkordb-host localhost --falkordb-port 6379 \\
        --falkordb-graph graphiti \\
        --out graph.mpf.json

    # Kuzu embedded:
    python -m tools.adapters.graphiti \\
        --backend kuzu --kuzu-db ~/.graphiti/kuzu.db \\
        --out graph.mpf.json

    # Scope to one tenant partition (Graphiti group_id):
    python -m tools.adapters.graphiti --neo4j bolt://... \\
        --group-id customer-42 --out tenant42.mpf.json

    # POST directly to a MNEMOS instance:
    python -m tools.adapters.graphiti --neo4j bolt://... \\
        --post http://mnemos:5002 --api-key $TOKEN

Key mapping (Graphiti → MPF):
    EntityNode                → records[] kind="memory",
                                 subcategory="graphiti_entity"
    EpisodicNode              → records[] kind="event",
                                 event_type="ingest_event",
                                 occurred_at=valid_at
    CommunityNode             → records[] kind="memory",
                                 subcategory="graphiti_community"
    SagaNode (if present)     → records[] kind="memory",
                                 subcategory="graphiti_saga"
    EntityEdge (RELATES_TO)   → envelope.kg_triples[] PRIMARY,
                                  with valid_from/valid_until/
                                  invalidated_at mapped natively,
                                  + records[] kind="fact" companion
                                  for consumers that don't read
                                  kg_triples[] (so no data is lost
                                  if a downstream only does records[])
    EpisodicEdge (MENTIONS)   → kg_triples[] predicate="mentions"
                                  (provenance links episode → entity)
    CommunityEdge (HAS_MEMBER)→ kg_triples[] predicate="has_member"
                                  (community → entity cluster links)

Temporal handling:
    Graphiti has TWO time axes per edge — created_at (when the
    system ingested the fact) and valid_at/invalid_at (when the
    fact was true in the world). MPF's kg_triples[] preserves
    both: occurred_at=created_at, valid_from=valid_at,
    valid_until=invalid_at (or expired_at, whichever is set).
    Bi-temporal invariance is preserved round-trip.

Tenancy:
    Graphiti partitions graphs by `group_id` (e.g., per-customer,
    per-agent). --group-id filters a single partition; without it
    all partitions are dumped and group_id is written into each
    record's tenancy axis (--tenancy-axis chooses owner_id vs
    namespace; default namespace, matching mempalace.py).

Design notes:
  * Edge records[] ("fact" kind) duplicate what kg_triples[]
    already carries. This is intentional: MNEMOS /v1/import treats
    them as first-class memories with content="<subject> <predicate>
    <object>" so facts land in search index even if a consumer
    never reads kg_triples[]. The same edge uuid is used for both
    (the record's id) so a re-importer can dedupe trivially.
  * Embeddings (name_embedding, fact_embedding) are NOT emitted —
    MPF importers regenerate with whatever embedder MNEMOS is
    configured for. Graphiti defaults to OpenAI ada; MNEMOS
    typically uses nomic-embed-text. Round-tripping embeddings
    across embedders is meaningless.
  * Idempotent: same graph snapshot + same group filter yields
    byte-identical envelope modulo exported_at.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

MPF_VERSION = "0.1.0"
# After translation, payloads are MNEMOS-native; declare mnemos-3.1
# so /v1/import's forward-compat check accepts them. Graphiti
# provenance lives in envelope.source_system + payload.metadata.
# graphiti.* round-trip blobs. (Same reasoning as mempalace.py; the
# mempalace adapter already has the post-mortem comment explaining
# why declaring a non-mnemos version silently drops all records.)
PAYLOAD_VERSION_MNEMOS = "mnemos-3.1"
SOURCE_SYSTEM = "graphiti"


# ─── Backend probes (drivers are optional; bail on use, not import) ─────────

try:
    import neo4j  # type: ignore
except ImportError:
    neo4j = None  # noqa: N816

try:
    import falkordb  # type: ignore
except ImportError:
    falkordb = None  # noqa: N816

try:
    import kuzu  # type: ignore
except ImportError:
    kuzu = None  # noqa: N816


# ─── Backend abstraction ────────────────────────────────────────────────────


class GraphitiBackend:
    """Uniform Cypher-ish reader over Neo4j / FalkorDB / Kuzu.

    Only the SELECT surface Graphiti uses is needed (MATCH with
    optional group_id filter). Each backend normalises its rows
    into plain dicts so downstream code never branches on driver.
    """

    def close(self) -> None: ...
    def entities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...
    def episodes(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...
    def communities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...
    def sagas(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...
    def entity_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...
    def episodic_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...
    def community_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]: ...


def _gid_clause(group_id: Optional[str], var: str = "n") -> Tuple[str, Dict[str, Any]]:
    if group_id is None:
        return "", {}
    return f" WHERE {var}.group_id = $group_id ", {"group_id": group_id}


class Neo4jBackend(GraphitiBackend):
    def __init__(self, uri: str, user: str, password: str, database: Optional[str] = None):
        if neo4j is None:
            raise SystemExit(
                "neo4j driver is required for Neo4j backend. Install with:\n"
                "  pip install neo4j"
            )
        self._driver = neo4j.GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass

    def _run(self, cy: str, **params: Any) -> Iterator[Dict[str, Any]]:
        with self._driver.session(database=self._database) as session:
            for rec in session.run(cy, **params):
                yield dict(rec)

    def entities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._run(
            f"MATCH (n:Entity){where}RETURN n, labels(n) AS labels",
            **params,
        )

    def episodes(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._run(f"MATCH (n:Episodic){where}RETURN n", **params)

    def communities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._run(f"MATCH (n:Community){where}RETURN n", **params)

    def sagas(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._run(f"MATCH (n:Saga){where}RETURN n", **params)

    def entity_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._run(
            "MATCH (s:Entity)-[e:RELATES_TO]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            **params,
        )

    def episodic_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._run(
            "MATCH (s:Episodic)-[e:MENTIONS]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            **params,
        )

    def community_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._run(
            "MATCH (s:Community)-[e:HAS_MEMBER]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            **params,
        )


class FalkorDBBackend(GraphitiBackend):
    def __init__(self, host: str, port: int, graph: str, password: Optional[str] = None):
        if falkordb is None:
            raise SystemExit(
                "falkordb client is required for FalkorDB backend. Install with:\n"
                "  pip install falkordb"
            )
        self._db = falkordb.FalkorDB(host=host, port=port, password=password)
        self._graph = self._db.select_graph(graph)

    def _query(self, cy: str, **params: Any) -> Iterator[Dict[str, Any]]:
        # FalkorDB's `Graph.query` returns a result whose `.result_set`
        # is a list of rows keyed positionally by `.header`. Normalise
        # to dict-rows keyed by the column alias.
        res = self._graph.query(cy, params)
        headers = [h[1].decode() if isinstance(h[1], bytes) else h[1] for h in res.header]
        for row in res.result_set:
            out: Dict[str, Any] = {}
            for k, v in zip(headers, row):
                # Nodes/edges come through as driver objects with
                # `.properties`; unwrap to dicts to match Neo4j's shape.
                props = getattr(v, "properties", None)
                out[k] = dict(props) if props is not None else v
            yield out

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def entities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(
            f"MATCH (n:Entity){where}RETURN n, labels(n) AS labels",
            **params,
        )

    def episodes(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(f"MATCH (n:Episodic){where}RETURN n", **params)

    def communities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(f"MATCH (n:Community){where}RETURN n", **params)

    def sagas(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(f"MATCH (n:Saga){where}RETURN n", **params)

    def entity_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._query(
            "MATCH (s:Entity)-[e:RELATES_TO]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            **params,
        )

    def episodic_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._query(
            "MATCH (s:Episodic)-[e:MENTIONS]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            **params,
        )

    def community_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._query(
            "MATCH (s:Community)-[e:HAS_MEMBER]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            **params,
        )


class KuzuBackend(GraphitiBackend):
    """Kuzu embedded store. Kuzu models Graphiti entity edges as
    *nodes* (RelatesToNode_) wrapped by RELATES_TO edges on both
    sides — per graphiti_core/nodes.py and edges.py. The unwrap
    queries below flatten that back to the logical subject/predicate/
    object shape before emission."""

    def __init__(self, path: str):
        if kuzu is None:
            raise SystemExit(
                "kuzu driver is required for Kuzu backend. Install with:\n"
                "  pip install kuzu"
            )
        self._db = kuzu.Database(path, read_only=True)
        self._conn = kuzu.Connection(self._db)

    def close(self) -> None:
        # Kuzu has no explicit close; rely on GC.
        self._conn = None  # type: ignore[assignment]
        self._db = None  # type: ignore[assignment]

    def _query(self, cy: str, params: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, Any]]:
        result = self._conn.execute(cy, params or {})
        cols = result.get_column_names()
        while result.has_next():
            row = result.get_next()
            yield {c: v for c, v in zip(cols, row)}

    def entities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(
            f"MATCH (n:Entity){where}RETURN n, n.labels AS labels", params,
        )

    def episodes(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(f"MATCH (n:Episodic){where}RETURN n", params)

    def communities(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(f"MATCH (n:Community){where}RETURN n", params)

    def sagas(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "n")
        yield from self._query(f"MATCH (n:Saga){where}RETURN n", params)

    def entity_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        # Kuzu: (s:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(t:Entity)
        where, params = _gid_clause(group_id, "e")
        yield from self._query(
            "MATCH (s:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            params,
        )

    def episodic_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._query(
            "MATCH (s:Episodic)-[e:MENTIONS]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            params,
        )

    def community_edges(self, group_id: Optional[str]) -> Iterator[Dict[str, Any]]:
        where, params = _gid_clause(group_id, "e")
        yield from self._query(
            "MATCH (s:Community)-[e:HAS_MEMBER]->(t:Entity)"
            + where
            + "RETURN e, s.uuid AS source_uuid, t.uuid AS target_uuid, "
              "s.name AS source_name, t.name AS target_name",
            params,
        )


def _open_backend(args: argparse.Namespace) -> GraphitiBackend:
    """Dispatch to the requested backend (mirrors mempalace._open_collection)."""
    backend = args.backend
    if backend == "neo4j":
        if not args.neo4j:
            raise SystemExit("--neo4j bolt-URI is required for neo4j backend")
        return Neo4jBackend(
            uri=args.neo4j,
            user=args.neo4j_user or os.environ.get("NEO4J_USER", "neo4j"),
            password=args.neo4j_password or os.environ.get("NEO4J_PASSWORD", ""),
            database=args.neo4j_database,
        )
    if backend == "falkordb":
        return FalkorDBBackend(
            host=args.falkordb_host,
            port=args.falkordb_port,
            graph=args.falkordb_graph,
            password=args.falkordb_password or os.environ.get("FALKORDB_PASSWORD"),
        )
    if backend == "kuzu":
        if not args.kuzu_db:
            raise SystemExit("--kuzu-db path is required for kuzu backend")
        return KuzuBackend(args.kuzu_db)
    raise SystemExit(f"Unknown backend: {backend}")


# ─── Row normalisation ──────────────────────────────────────────────────────


def _unwrap_node(val: Any) -> Dict[str, Any]:
    """Neo4j `Node` → dict; pass through dict unchanged."""
    props = getattr(val, "_properties", None)
    if props is not None:
        return dict(props)
    # neo4j v5 `Node` is itself mapping-like
    if hasattr(val, "items") and not isinstance(val, dict):
        try:
            return dict(val.items())
        except Exception:
            pass
    return dict(val) if isinstance(val, dict) else {}


def _isoformat(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    # neo4j.time.DateTime has .iso_format()
    iso = getattr(v, "iso_format", None)
    if callable(iso):
        return iso()
    # falkordb returns numeric epoch millis for timestamps
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(v)


# ─── Record / triple builders ───────────────────────────────────────────────


def _tenancy_payload(group_id: Optional[str], axis: str) -> Dict[str, Any]:
    value = group_id or "default"
    return {axis: value}


def _entity_to_record(row: Dict[str, Any], tenancy_axis: str) -> Dict[str, Any]:
    n = _unwrap_node(row.get("n"))
    labels = row.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    labels = [lbl for lbl in labels if lbl != "Entity"]
    name = n.get("name", "")
    summary = n.get("summary", "") or ""
    body = f"{name}\n\n{summary}" if summary else name

    payload: Dict[str, Any] = {
        "content": body,
        "category": "graphiti",
        "subcategory": "graphiti_entity",
        "created": _isoformat(n.get("created_at")),
        **_tenancy_payload(n.get("group_id"), tenancy_axis),
        "metadata": {
            "graphiti": {
                "kind": "EntityNode",
                "uuid": n.get("uuid"),
                "name": name,
                "group_id": n.get("group_id"),
                "labels": labels,
                "attributes": _coerce_attrs(n.get("attributes")),
                "has_name_embedding": n.get("name_embedding") is not None,
            }
        },
    }
    return {
        "id": n.get("uuid"),
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


def _episode_to_record(row: Dict[str, Any], tenancy_axis: str) -> Dict[str, Any]:
    n = _unwrap_node(row.get("n"))
    payload: Dict[str, Any] = {
        "content": n.get("content", "") or "",
        "category": "graphiti",
        "subcategory": "graphiti_episode",
        "event_type": "ingest_event",
        "occurred_at": _isoformat(n.get("valid_at")) or _isoformat(n.get("created_at")),
        "actor": n.get("name"),
        "created": _isoformat(n.get("created_at")),
        **_tenancy_payload(n.get("group_id"), tenancy_axis),
        "metadata": {
            "graphiti": {
                "kind": "EpisodicNode",
                "uuid": n.get("uuid"),
                "name": n.get("name"),
                "group_id": n.get("group_id"),
                "source": n.get("source"),
                "source_description": n.get("source_description"),
                "valid_at": _isoformat(n.get("valid_at")),
                "entity_edges": n.get("entity_edges") or [],
                "episode_metadata": n.get("episode_metadata"),
            }
        },
    }
    return {
        "id": n.get("uuid"),
        "kind": "event",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


def _community_to_record(row: Dict[str, Any], tenancy_axis: str) -> Dict[str, Any]:
    n = _unwrap_node(row.get("n"))
    name = n.get("name", "")
    summary = n.get("summary", "") or ""
    body = f"{name}\n\n{summary}" if summary else name
    payload: Dict[str, Any] = {
        "content": body,
        "category": "graphiti",
        "subcategory": "graphiti_community",
        "created": _isoformat(n.get("created_at")),
        **_tenancy_payload(n.get("group_id"), tenancy_axis),
        "metadata": {
            "graphiti": {
                "kind": "CommunityNode",
                "uuid": n.get("uuid"),
                "name": name,
                "group_id": n.get("group_id"),
            }
        },
    }
    return {
        "id": n.get("uuid"),
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


def _saga_to_record(row: Dict[str, Any], tenancy_axis: str) -> Dict[str, Any]:
    n = _unwrap_node(row.get("n"))
    name = n.get("name", "")
    summary = n.get("summary", "") or ""
    body = f"{name}\n\n{summary}" if summary else name
    payload: Dict[str, Any] = {
        "content": body,
        "category": "graphiti",
        "subcategory": "graphiti_saga",
        "created": _isoformat(n.get("created_at")),
        **_tenancy_payload(n.get("group_id"), tenancy_axis),
        "metadata": {
            "graphiti": {
                "kind": "SagaNode",
                "uuid": n.get("uuid"),
                "name": name,
                "group_id": n.get("group_id"),
                "first_episode_uuid": n.get("first_episode_uuid"),
                "last_episode_uuid": n.get("last_episode_uuid"),
                "last_summarized_at": _isoformat(n.get("last_summarized_at")),
            }
        },
    }
    return {
        "id": n.get("uuid"),
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


def _coerce_attrs(raw: Any) -> Dict[str, Any]:
    """Graphiti stashes per-node attributes as JSON-stringified blob
    on Kuzu and as top-level keys on Neo4j. Normalise to a dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {"_raw": out}
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {"_raw": raw}


def _entity_edge_triple(row: Dict[str, Any]) -> Dict[str, Any]:
    e = _unwrap_node(row.get("e"))
    # expired_at and invalid_at are semantically overlapping in
    # Graphiti: expired_at is set when the fact is superseded by a
    # later ingestion; invalid_at is the claim "this stopped being
    # true in the world at T". Either zeroes the validity window —
    # prefer invalid_at (world-time) but fall back to expired_at
    # so we don't silently drop the signal.
    valid_until = _isoformat(e.get("invalid_at")) or _isoformat(e.get("expired_at"))
    return {
        "id": e.get("uuid"),
        "subject_id": row.get("source_uuid"),
        "subject": row.get("source_name") or row.get("source_uuid"),
        "predicate": e.get("name") or "relates_to",
        "object_id": row.get("target_uuid"),
        "object": row.get("target_name") or row.get("target_uuid"),
        "valid_from": _isoformat(e.get("valid_at")),
        "valid_until": valid_until,
        "occurred_at": _isoformat(e.get("created_at")),
        "source_system": SOURCE_SYSTEM,
        "metadata": {
            "graphiti": {
                "kind": "EntityEdge",
                "uuid": e.get("uuid"),
                "fact": e.get("fact"),
                "group_id": e.get("group_id"),
                "episodes": e.get("episodes") or [],
                "reference_time": _isoformat(e.get("reference_time")),
                "expired_at": _isoformat(e.get("expired_at")),
                "attributes": _coerce_attrs(e.get("attributes")),
                "has_fact_embedding": e.get("fact_embedding") is not None,
            }
        },
    }


def _entity_edge_record(triple: Dict[str, Any], tenancy_axis: str) -> Dict[str, Any]:
    """Companion 'fact' kind record for consumers that don't read
    kg_triples[]. Uses the same id as the triple so dedupe on
    re-import is trivial. Content is the Graphiti-minted `fact`
    sentence when present, otherwise a synthesized S/P/O line."""
    meta = triple["metadata"]["graphiti"]
    fact = meta.get("fact") or f"{triple['subject']} {triple['predicate']} {triple['object']}"
    payload: Dict[str, Any] = {
        "content": fact,
        "category": "graphiti",
        "subcategory": "graphiti_edge",
        "subject": triple["subject"],
        "predicate": triple["predicate"],
        "object": triple["object"],
        "valid_from": triple.get("valid_from"),
        "valid_until": triple.get("valid_until"),
        "created": triple.get("occurred_at"),
        **_tenancy_payload(meta.get("group_id"), tenancy_axis),
        "metadata": {"graphiti": meta},
    }
    return {
        "id": triple["id"],
        "kind": "fact",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


def _episodic_edge_triple(row: Dict[str, Any]) -> Dict[str, Any]:
    e = _unwrap_node(row.get("e"))
    return {
        "id": e.get("uuid"),
        "subject_id": row.get("source_uuid"),
        "subject": row.get("source_name") or row.get("source_uuid"),
        "predicate": "mentions",
        "object_id": row.get("target_uuid"),
        "object": row.get("target_name") or row.get("target_uuid"),
        "occurred_at": _isoformat(e.get("created_at")),
        "source_system": SOURCE_SYSTEM,
        "metadata": {
            "graphiti": {
                "kind": "EpisodicEdge",
                "uuid": e.get("uuid"),
                "group_id": e.get("group_id"),
            }
        },
    }


def _community_edge_triple(row: Dict[str, Any]) -> Dict[str, Any]:
    e = _unwrap_node(row.get("e"))
    return {
        "id": e.get("uuid"),
        "subject_id": row.get("source_uuid"),
        "subject": row.get("source_name") or row.get("source_uuid"),
        "predicate": "has_member",
        "object_id": row.get("target_uuid"),
        "object": row.get("target_name") or row.get("target_uuid"),
        "occurred_at": _isoformat(e.get("created_at")),
        "source_system": SOURCE_SYSTEM,
        "metadata": {
            "graphiti": {
                "kind": "CommunityEdge",
                "uuid": e.get("uuid"),
                "group_id": e.get("group_id"),
            }
        },
    }


# ─── Streaming envelope assembly ────────────────────────────────────────────


def iter_records(
    backend: GraphitiBackend,
    *,
    group_id: Optional[str] = None,
    tenancy_axis: str = "namespace",
    emit_edge_records: bool = True,
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Stream ('records' | 'kg_triples', payload) pairs.

    Yields everything the envelope needs in a deterministic order:
    entities, episodes, communities, sagas (nodes), then the three
    edge kinds. `_kg` pairs go into envelope.kg_triples[]; others
    into records[]."""
    for row in backend.entities(group_id):
        yield "records", _entity_to_record(row, tenancy_axis)
    for row in backend.episodes(group_id):
        yield "records", _episode_to_record(row, tenancy_axis)
    for row in backend.communities(group_id):
        yield "records", _community_to_record(row, tenancy_axis)
    for row in backend.sagas(group_id):
        yield "records", _saga_to_record(row, tenancy_axis)

    for row in backend.entity_edges(group_id):
        triple = _entity_edge_triple(row)
        yield "kg_triples", triple
        if emit_edge_records:
            yield "records", _entity_edge_record(triple, tenancy_axis)
    for row in backend.episodic_edges(group_id):
        yield "kg_triples", _episodic_edge_triple(row)
    for row in backend.community_edges(group_id):
        yield "kg_triples", _community_edge_triple(row)


def build_envelope(
    backend: GraphitiBackend,
    *,
    source_instance: Optional[str] = None,
    group_id: Optional[str] = None,
    tenancy_axis: str = "namespace",
    emit_edge_records: bool = True,
) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    triples: List[Dict[str, Any]] = []
    for bucket, payload in iter_records(
        backend,
        group_id=group_id,
        tenancy_axis=tenancy_axis,
        emit_edge_records=emit_edge_records,
    ):
        (records if bucket == "records" else triples).append(payload)
    return {
        "mpf_version": MPF_VERSION,
        "source_system": SOURCE_SYSTEM,
        "source_version": _detect_graphiti_version(),
        "source_instance": source_instance,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "kg_triple_count": len(triples),
        "records": records,
        "kg_triples": triples,
    }


def _detect_graphiti_version() -> Optional[str]:
    try:
        from graphiti_core import __version__  # type: ignore
        return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version  # py3.8+
        return version("graphiti-core")
    except Exception:
        return None


# ─── MPF → MNEMOS POST (optional) ───────────────────────────────────────────


def _post_to_mnemos(
    envelope: Dict[str, Any],
    endpoint: str,
    api_key: str,
    *,
    batch_size: int = 200,
) -> Dict[str, int]:
    """POST to MNEMOS /v1/import?preserve_owner=true in batches.

    Triples are attached to the first batch so MNEMOS links them
    against records that already exist (entities are always emitted
    before edges by iter_records, and records[] within a batch is
    committed before kg_triples[] server-side). If the triple count
    is large the triples ride along on the final batch instead — a
    later batch would have no records[] to anchor against."""
    records = envelope.get("records") or []
    triples = envelope.get("kg_triples") or []
    totals = {"imported": 0, "skipped": 0, "failed": 0, "triples": 0}
    base = endpoint.rstrip("/") + "/v1/import?preserve_owner=true"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    n_batches = max(1, (len(records) + batch_size - 1) // batch_size)
    for idx, start in enumerate(range(0, max(len(records), 1), batch_size)):
        is_last = idx == n_batches - 1
        chunk: Dict[str, Any] = {
            "mpf_version": envelope["mpf_version"],
            "source_system": envelope.get("source_system"),
            "source_version": envelope.get("source_version"),
            "exported_at": envelope["exported_at"],
            "records": records[start:start + batch_size],
        }
        if is_last and triples:
            chunk["kg_triples"] = triples
            totals["triples"] = len(triples)
        data = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(base, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read())
                for k in ("imported", "skipped", "failed"):
                    totals[k] += int(body.get(k, 0))
                print(
                    f"  batch {idx + 1}/{n_batches}: "
                    f"imported={body.get('imported')} "
                    f"skipped={body.get('skipped')} "
                    f"failed={body.get('failed')}"
                    + (f" triples={len(triples)}" if is_last and triples else ""),
                    file=sys.stderr,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"  WARNING /v1/import HTTP {e.code}: {body}", file=sys.stderr)
            totals["failed"] += len(chunk["records"])
    return totals


# ─── CLI ────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="tools.adapters.graphiti",
        description=(
            "Graphiti → MPF v0.1 adapter (CHARON). Reads a Graphiti "
            "temporal knowledge graph (Neo4j / FalkorDB / Kuzu) "
            "directly and emits an MPF envelope with records[] + "
            "kg_triples[]. Optionally POSTs to MNEMOS /v1/import."
        ),
    )
    p.add_argument("--backend", choices=("neo4j", "falkordb", "kuzu"),
                   default="neo4j",
                   help="Backing graph store (default: neo4j).")
    # Neo4j
    p.add_argument("--neo4j", default=None, metavar="URI",
                   help="Neo4j bolt URI, e.g. bolt://localhost:7687")
    p.add_argument("--neo4j-user", default=None,
                   help="Neo4j username (or env NEO4J_USER; defaults to 'neo4j').")
    p.add_argument("--neo4j-password", default=None,
                   help="Neo4j password (or env NEO4J_PASSWORD).")
    p.add_argument("--neo4j-database", default=None,
                   help="Neo4j database name (default: driver default).")
    # FalkorDB
    p.add_argument("--falkordb-host", default="localhost")
    p.add_argument("--falkordb-port", type=int, default=6379)
    p.add_argument("--falkordb-graph", default="graphiti",
                   help="FalkorDB graph name (default: graphiti).")
    p.add_argument("--falkordb-password", default=None,
                   help="FalkorDB password (or env FALKORDB_PASSWORD).")
    # Kuzu
    p.add_argument("--kuzu-db", default=None, metavar="PATH",
                   help="Path to Kuzu database directory.")
    # Filter / tenancy
    p.add_argument("--group-id", default=None,
                   help="Restrict export to a single Graphiti group_id "
                        "(tenant partition). Default: all partitions.")
    p.add_argument("--tenancy-axis", choices=("owner_id", "namespace"),
                   default="namespace",
                   help="Which MNEMOS tenancy axis to write Graphiti "
                        "group_id into (default: namespace).")
    p.add_argument("--no-edge-records", action="store_true",
                   help="Emit entity edges ONLY as kg_triples[] — skip "
                        "the companion 'fact' records[] entries. Smaller "
                        "envelope; requires the importer to consume "
                        "kg_triples[].")
    # Output
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Write MPF envelope to this file ('-' for stdout).")
    p.add_argument("--post", default=None, metavar="URL",
                   help="POST the envelope to a MNEMOS /v1/import endpoint.")
    p.add_argument("--api-key", default=None,
                   help="Bearer token for MNEMOS auth (needed with --post).")
    p.add_argument("--source-instance", default=None,
                   help="Diagnostic label written into the envelope.")
    args = p.parse_args(argv)

    if not (args.out or args.post):
        print("ERROR: pass --out PATH or --post URL", file=sys.stderr)
        return 2
    if args.post and not args.api_key:
        print("ERROR: --post requires --api-key", file=sys.stderr)
        return 2

    source = _open_backend(args)
    try:
        t0 = time.time()
        envelope = build_envelope(
            source,
            source_instance=args.source_instance,
            group_id=args.group_id,
            tenancy_axis=args.tenancy_axis,
            emit_edge_records=not args.no_edge_records,
        )
        elapsed = time.time() - t0
    finally:
        source.close()

    print(
        f"read {envelope['record_count']} records + "
        f"{envelope['kg_triple_count']} kg_triples from graphiti "
        f"in {elapsed:.1f}s",
        file=sys.stderr,
    )

    if args.out:
        if args.out == "-":
            json.dump(envelope, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(envelope, f, indent=2, ensure_ascii=False)
            print(f"wrote envelope to {args.out}", file=sys.stderr)

    if args.post:
        totals = _post_to_mnemos(envelope, args.post, args.api_key)
        print(
            f"POST complete: "
            f"imported={totals['imported']} "
            f"skipped={totals['skipped']} "
            f"failed={totals['failed']} "
            f"triples={totals['triples']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
