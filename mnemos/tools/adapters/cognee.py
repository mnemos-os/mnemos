#!/usr/bin/env python3
"""
tools/adapters/cognee.py — CHARON adapter for Cognee knowledge engines.

Converts a Cognee instance (graph DB + vector DB + relational doc
catalog, scoped by user/dataset) into an MPF v0.1 envelope. Cognee
stores its content across three backends tied together by a shared
DataPoint UUID: a graph database (Neo4j / Kuzu / Postgres / Neptune)
holds DocumentChunks, Entities, EntityTypes and their relationships;
a vector database (LanceDB / Qdrant / Weaviate / pgvector / others)
holds the embeddings; and a relational SQLAlchemy catalog holds the
Dataset / Data (document) rows with user/tenant ownership.

This adapter uses Cognee's own Python API wherever possible:

    * ``cognee.datasets.list_datasets`` / ``list_data`` — source-of-truth
      document catalogue, with ``owner_id`` and ``tenant_id`` for tenancy.
    * ``cognee.infrastructure.databases.graph.get_graph_engine`` →
      ``await graph.get_graph_data()`` — returns ``(nodes, edges)`` for
      the current graph (DocumentChunks, Entities, EntityTypes, edges).

Usage::

    python -m mnemos.tools.adapters.cognee --out cognee.mpf.json
    python -m mnemos.tools.adapters.cognee --dataset my_dataset --out cognee.mpf.json
    python -m mnemos.tools.adapters.cognee --post http://mnemos:5002 --api-key $TOKEN
    python -m mnemos.tools.adapters.cognee --out - | python -m mnemos.tools.mpf_validate --file -

Key mapping (Cognee → MPF):

    Data (document row)    → records[] kind="document"
    DocumentChunk node     → records[] kind="memory" (source_record_ids=[doc_id])
    Entity node            → records[] kind="memory" subcategory="cognee_entity"
    EntityType node        → records[] kind="memory" subcategory="cognee_entity_type"
    Summary / Event nodes  → records[] kind="memory" subcategory="cognee_<type>"
    chunk-is_part_of-doc   → envelope.relations[] from=chunk to=doc rel="is_part_of"
    chunk-contains-entity  → envelope.relations[] from=chunk to=entity rel="contains"
    entity-is_a-type       → envelope.kg_triples[] subj=entity pred="is_a" obj=type
    other LLM-extracted    → envelope.kg_triples[] (subj, pred=rel_name, obj)
    Dataset                → payload.namespace (configurable tenancy axis)
    Dataset.owner_id       → payload.owner_id when --tenancy-axis=owner_id

Design notes:

  * Cognee relation names are *LLM-inferred from content* rather than
    a fixed vocabulary — we pass them through verbatim as the triple
    predicate, and preserve any edge ``properties`` under
    ``metadata.cognee.edge_properties``.
  * Provenance chain (entity → chunk → document) is kept via two
    parallel mechanisms: ``envelope.relations[]`` captures the
    structural chunk→doc and chunk→entity edges (1:N), while
    ``source_record_ids`` on each chunk record points back at its
    parent document so single-record importers still resolve
    provenance without parsing the relation table.
  * Vector embeddings are NOT emitted — MPF spec permits regeneration
    at import time and Cognee's embeddings are model-coupled.
  * Idempotent: same Cognee snapshot emits byte-identical envelope
    modulo ``exported_at``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

MPF_VERSION = "0.1.0"
# Adapter translates Cognee nodes → MNEMOS memory shape. Cognee-native
# fields survive round-trip under payload.metadata.cognee.*. The
# envelope declares mnemos-3.1 because that's what /v1/import accepts;
# source_system="cognee" marks provenance at envelope level.
PAYLOAD_VERSION_MNEMOS = "mnemos-3.1"
SOURCE_SYSTEM = "cognee"

# Cognee is required — we drive its Python API directly. Bail early
# with a clear message if it isn't importable.
try:
    import cognee  # type: ignore  # noqa: F401
    _COGNEE_AVAILABLE = True
except ImportError:
    _COGNEE_AVAILABLE = False


# #181: removed `_STRUCTURAL_EDGES = {"is_part_of", "contains",
# "made_from"}` — defined but never referenced. Earlier rounds may
# have used this set to short-circuit edge classification, but the
# current code path treats every Cognee-emitted edge as a candidate
# kg_triple predicate.


# ─── Cognee access ───────────────────────────────────────────────────────────


def _require_cognee() -> None:
    if not _COGNEE_AVAILABLE:
        raise SystemExit(
            "cognee is required. Install with:\n"
            "  pip install cognee\n"
            "and point it at your configured graph + vector stores via "
            "the same env vars the Cognee runtime uses (GRAPH_DATABASE_*, "
            "VECTOR_DB_*, DB_*)."
        )


async def _load_datasets(dataset_names: Optional[List[str]]) -> List[Any]:
    """Return the Dataset rows that should be exported.

    When ``dataset_names`` is None, every dataset the current user can
    read is exported. When names are supplied, only matching datasets
    are kept (by ``name`` match, case-sensitive — Cognee datasets are
    name-unique per owner).
    """
    from cognee.api.v1.datasets import datasets as datasets_api  # type: ignore

    all_datasets = await datasets_api.list_datasets()
    if not dataset_names:
        return list(all_datasets)
    wanted = set(dataset_names)
    return [ds for ds in all_datasets if getattr(ds, "name", None) in wanted]


async def _load_dataset_data(dataset: Any) -> List[Any]:
    """Return the Data (document) rows for a given dataset."""
    from cognee.api.v1.datasets import datasets as datasets_api  # type: ignore

    return list(await datasets_api.list_data(dataset.id))


async def _load_graph_snapshot() -> Tuple[List[Any], List[Any]]:
    """Return ``(nodes, edges)`` from Cognee's graph engine.

    Cognee keeps one graph across all datasets for a given deployment;
    dataset scoping is applied post-hoc by filtering on the ``dataset_id``
    or ``belongs_to_set`` fields attached to each node. We pull the
    whole snapshot here and filter in the record-building step so the
    network round-trip is paid once per export.
    """
    from cognee.infrastructure.databases.graph import (  # type: ignore
        get_graph_engine,
    )

    graph = await get_graph_engine()
    # get_graph_data is the canonical dump hook on GraphDBInterface.
    nodes, edges = await graph.get_graph_data()
    return list(nodes or []), list(edges or [])


# ─── Node + edge normalisation ───────────────────────────────────────────────


def _node_id(node: Any) -> str:
    """Extract a stable string id from whatever the backend returned.

    Cognee's graph adapters hand back either raw dicts (Kuzu / Postgres
    hybrid) or tuples of ``(id, properties)`` (Neo4j). DataPoint ids are
    always UUIDs; we stringify defensively.
    """
    if isinstance(node, tuple) and len(node) == 2:
        nid, _props = node
        return str(nid)
    if isinstance(node, dict):
        for key in ("id", "node_id", "uuid"):
            if key in node and node[key] is not None:
                return str(node[key])
    return str(getattr(node, "id", node))


def _node_props(node: Any) -> Dict[str, Any]:
    """Normalise a node into its properties dict."""
    if isinstance(node, tuple) and len(node) == 2:
        _nid, props = node
        return dict(props or {})
    if isinstance(node, dict):
        # Some adapters nest everything under "properties", others flatten.
        if "properties" in node and isinstance(node["properties"], dict):
            merged = dict(node["properties"])
            for k in ("id", "type"):
                if k in node and k not in merged:
                    merged[k] = node[k]
            return merged
        return dict(node)
    # DataPoint / BaseModel instance
    if hasattr(node, "model_dump"):
        return node.model_dump(mode="json")
    if hasattr(node, "dict"):
        return node.dict()
    return {}


def _edge_parts(edge: Any) -> Tuple[str, str, str, Dict[str, Any]]:
    """Return ``(source_id, target_id, relationship_name, properties)``."""
    if isinstance(edge, (list, tuple)):
        if len(edge) == 4:
            s, t, r, p = edge
            return str(s), str(t), str(r or ""), dict(p or {})
        if len(edge) == 3:
            s, t, r = edge
            return str(s), str(t), str(r or ""), {}
    if isinstance(edge, dict):
        s = edge.get("source") or edge.get("source_node_id") or edge.get("from")
        t = edge.get("target") or edge.get("target_node_id") or edge.get("to")
        r = (
            edge.get("relationship_name")
            or edge.get("rel_name")
            or edge.get("type")
            or edge.get("label")
            or ""
        )
        p = edge.get("properties") or {
            k: v for k, v in edge.items()
            if k not in {
                "source", "source_node_id", "from",
                "target", "target_node_id", "to",
                "relationship_name", "rel_name", "type", "label",
                "properties",
            }
        }
        return str(s), str(t), str(r), dict(p or {})
    return "", "", "", {}


def _node_type(props: Dict[str, Any]) -> str:
    """Cognee writes the class name into ``type`` / ``_type``. Default
    to ``DataPoint`` when missing so unknown subclasses still flow
    through as generic memories rather than being dropped."""
    return str(
        props.get("type")
        or props.get("_type")
        or props.get("node_type")
        or "DataPoint"
    )


# ─── Data (document) → MPF document record ──────────────────────────────────


def _data_to_record(
    data_row: Any,
    dataset: Any,
    *,
    tenancy_axis: str,
) -> Dict[str, Any]:
    """Shape a Cognee Data row (document catalogue entry) into an MPF
    record of kind=document."""
    # Data is a SQLAlchemy model — to_json returns the canonical dict.
    if hasattr(data_row, "to_json"):
        data_dict = data_row.to_json()
    elif isinstance(data_row, dict):
        data_dict = dict(data_row)
    else:
        data_dict = {k: getattr(data_row, k, None) for k in (
            "id", "name", "extension", "mime_type", "raw_data_location",
            "content_hash", "token_count", "data_size",
            "created_at", "updated_at", "owner_id", "tenant_id",
        )}

    doc_id = str(data_dict.get("id"))
    ds_name = getattr(dataset, "name", None) or str(getattr(dataset, "id", ""))
    owner = str(data_dict.get("owner_id") or getattr(dataset, "owner_id", "") or "")
    tenant = str(data_dict.get("tenant_id") or getattr(dataset, "tenant_id", "") or "")

    payload: Dict[str, Any] = {
        "name": data_dict.get("name") or f"cognee-doc-{doc_id}",
        "category": "document",
        "mime_type": data_dict.get("mime_type"),
        "content_hash": data_dict.get("content_hash"),
        "metadata": {
            "cognee": {
                "data_id": doc_id,
                "dataset_id": str(getattr(dataset, "id", "")),
                "dataset_name": ds_name,
                "extension": data_dict.get("extension"),
                "raw_data_location": data_dict.get("raw_data_location"),
                "token_count": data_dict.get("token_count"),
                "data_size": data_dict.get("data_size"),
                "owner_id": owner or None,
                "tenant_id": tenant or None,
                "created_at": data_dict.get("created_at"),
                "updated_at": data_dict.get("updated_at"),
            },
        },
    }
    if tenancy_axis == "owner_id" and owner:
        payload["owner_id"] = owner
    else:
        payload["namespace"] = ds_name

    return {
        "id": doc_id,
        "kind": "document",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }


# ─── Graph node → MPF memory record ──────────────────────────────────────────


def _chunk_to_record(
    node_id: str,
    props: Dict[str, Any],
    parent_doc_id: Optional[str],
    *,
    ds_name: str,
    owner: Optional[str],
    tenancy_axis: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "content": props.get("text") or props.get("content") or "",
        "category": "document_chunk",
        "subcategory": "cognee_chunk",
        "metadata": {
            "cognee": {
                "node_type": "DocumentChunk",
                "chunk_index": props.get("chunk_index"),
                "chunk_size": props.get("chunk_size"),
                "cut_type": props.get("cut_type"),
                "is_part_of": props.get("is_part_of") or parent_doc_id,
                "version": props.get("version"),
                "topological_rank": props.get("topological_rank"),
                "importance_weight": props.get("importance_weight"),
                "dataset_name": ds_name,
            },
        },
    }
    if tenancy_axis == "owner_id" and owner:
        payload["owner_id"] = owner
    else:
        payload["namespace"] = ds_name

    record: Dict[str, Any] = {
        "id": node_id,
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }
    if parent_doc_id:
        record["source_record_ids"] = [parent_doc_id]
    return record


def _entity_like_to_record(
    node_id: str,
    props: Dict[str, Any],
    node_type: str,
    *,
    ds_name: str,
    owner: Optional[str],
    tenancy_axis: str,
    source_chunk_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Entity / EntityType / Event / Summary / NodeSet → memory record
    with a subcategory that preserves the Cognee class name."""
    subcat_map = {
        "Entity": "cognee_entity",
        "EntityType": "cognee_entity_type",
        "Event": "cognee_event",
        "TextSummary": "cognee_summary",
        "CodeSummary": "cognee_code_summary",
        "NodeSet": "cognee_node_set",
    }
    subcategory = subcat_map.get(node_type, f"cognee_{node_type.lower()}")

    # Prefer an explicit description/content field; fall back to name.
    name = props.get("name") or ""
    description = props.get("description") or props.get("text") or ""
    if name and description:
        content = f"{name}: {description}"
    else:
        content = description or name or ""

    payload: Dict[str, Any] = {
        "content": content,
        "category": "entity" if node_type in ("Entity", "EntityType") else "knowledge",
        "subcategory": subcategory,
        "metadata": {
            "cognee": {
                "node_type": node_type,
                "name": name or None,
                "description": description or None,
                "is_a": props.get("is_a"),
                "version": props.get("version"),
                "topological_rank": props.get("topological_rank"),
                "importance_weight": props.get("importance_weight"),
                "dataset_name": ds_name,
            },
        },
    }
    if tenancy_axis == "owner_id" and owner:
        payload["owner_id"] = owner
    else:
        payload["namespace"] = ds_name

    record: Dict[str, Any] = {
        "id": node_id,
        "kind": "memory",
        "payload_version": PAYLOAD_VERSION_MNEMOS,
        "payload": payload,
    }
    if source_chunk_ids:
        record["source_record_ids"] = list(source_chunk_ids)
    return record


# ─── Envelope assembly ───────────────────────────────────────────────────────


async def _collect(
    dataset_names: Optional[List[str]],
    *,
    tenancy_axis: str,
) -> Tuple[
    List[Dict[str, Any]],       # records
    List[Dict[str, Any]],       # relations
    List[Dict[str, Any]],       # kg_triples
    Dict[str, Any],             # diagnostics
]:
    """Main extraction pipeline. Returns records + relations + triples +
    a small diagnostics dict for the envelope footer."""
    _require_cognee()

    datasets = await _load_datasets(dataset_names)
    if not datasets:
        return [], [], [], {"datasets": 0}

    # 1) Relational catalogue — datasets and their Data (doc) rows.
    records: List[Dict[str, Any]] = []
    doc_ids: Dict[str, Tuple[str, Optional[str]]] = {}  # doc_id → (ds_name, owner)
    ds_owner: Dict[str, Tuple[str, Optional[str]]] = {}  # ds_id → (ds_name, owner)

    for ds in datasets:
        ds_name = getattr(ds, "name", None) or str(getattr(ds, "id", ""))
        owner = str(getattr(ds, "owner_id", "") or "") or None
        ds_owner[str(getattr(ds, "id", ""))] = (ds_name, owner)
        for data_row in await _load_dataset_data(ds):
            rec = _data_to_record(data_row, ds, tenancy_axis=tenancy_axis)
            records.append(rec)
            doc_ids[rec["id"]] = (ds_name, owner)

    # 2) Graph snapshot. Cognee keeps one graph across datasets; we
    #    filter by dataset membership where node properties permit.
    nodes, edges = await _load_graph_snapshot()

    # Build an id→props map for O(1) lookup when walking edges.
    node_index: Dict[str, Dict[str, Any]] = {}
    node_type_index: Dict[str, str] = {}
    for n in nodes:
        nid = _node_id(n)
        if not nid:
            continue
        props = _node_props(n)
        node_index[nid] = props
        node_type_index[nid] = _node_type(props)

    # 3) Edge pass — split into structural (relations) and semantic
    #    (kg_triples). Also build chunk→doc and entity→chunk lookups
    #    so the record builders can populate source_record_ids.
    relations: List[Dict[str, Any]] = []
    kg_triples: List[Dict[str, Any]] = []
    chunk_to_doc: Dict[str, str] = {}
    entity_to_chunks: Dict[str, List[str]] = {}

    for edge in edges:
        src, tgt, rel, props = _edge_parts(edge)
        if not src or not tgt:
            continue
        src_type = node_type_index.get(src, "")

        # Canonical structural chunk → document.
        if rel == "is_part_of" and src_type == "DocumentChunk":
            chunk_to_doc[src] = tgt
            relations.append({
                "from": src, "rel": "is_part_of", "to": tgt,
                "metadata": {"cognee": {"edge_properties": props}} if props else {},
            })
            continue

        # Canonical structural chunk → entity.
        if rel == "contains" and src_type == "DocumentChunk":
            entity_to_chunks.setdefault(tgt, []).append(src)
            relations.append({
                "from": src, "rel": "contains", "to": tgt,
                "metadata": {"cognee": {"edge_properties": props}} if props else {},
            })
            continue

        # Everything else (is_a, mentions, made_from, plus LLM-inferred
        # edges) lands in kg_triples with the relationship name as the
        # predicate, verbatim. source_record_ids wire back the chunk
        # that originated the statement when the edge carried one.
        triple: Dict[str, Any] = {
            "subject": src,
            "predicate": rel or "related_to",
            "object": tgt,
        }
        source_chunk = props.get("source_chunk_id") or props.get("chunk_id")
        if source_chunk:
            triple["source_record_ids"] = [str(source_chunk)]
        if props:
            triple["metadata"] = {"cognee": {"edge_properties": props}}
        kg_triples.append(triple)

    # 4) Node pass — turn DocumentChunks / Entities / EntityTypes / etc.
    #    into memory records. Skip Document-class graph nodes (they're
    #    already covered by the relational Data rows) to avoid duplicate
    #    ids in the envelope.
    doc_graph_types = {"Document", "TextDocument", "PdfDocument", "AudioDocument",
                       "ImageDocument", "CsvDocument", "UnstructuredDocument",
                       "DltRowDocument"}

    for nid, props in node_index.items():
        ntype = node_type_index[nid]
        if ntype in doc_graph_types:
            continue  # already represented as kind=document from Data catalogue
        # Dataset lookup — Cognee attaches dataset_id on nodes when the
        # graph engine supports it; otherwise we fall back to the first
        # dataset we saw (single-dataset deployments are the norm).
        ds_id = str(props.get("dataset_id") or "")
        ds_name, owner = ds_owner.get(ds_id, (
            next(iter(ds_owner.values()), ("default", None))
        ))

        if ntype == "DocumentChunk":
            parent_doc = chunk_to_doc.get(nid) or str(props.get("is_part_of") or "") or None
            records.append(_chunk_to_record(
                nid, props, parent_doc,
                ds_name=ds_name, owner=owner, tenancy_axis=tenancy_axis,
            ))
        else:
            records.append(_entity_like_to_record(
                nid, props, ntype,
                ds_name=ds_name, owner=owner, tenancy_axis=tenancy_axis,
                source_chunk_ids=entity_to_chunks.get(nid),
            ))

    diagnostics = {
        "datasets": len(datasets),
        "documents": len(doc_ids),
        "graph_nodes": len(node_index),
        "graph_edges": len(edges),
        "relations": len(relations),
        "kg_triples": len(kg_triples),
    }
    return records, relations, kg_triples, diagnostics


def _detect_cognee_version() -> Optional[str]:
    try:
        from cognee.version import __version__  # type: ignore
        return __version__
    except Exception:
        try:
            import cognee  # type: ignore
            return getattr(cognee, "__version__", None)
        except Exception:
            return None


def build_envelope(
    dataset_names: Optional[List[str]] = None,
    *,
    source_instance: Optional[str] = None,
    tenancy_axis: str = "namespace",
) -> Dict[str, Any]:
    """Assemble a full MPF envelope from a Cognee deployment snapshot."""
    records, relations, kg_triples, diag = asyncio.run(_collect(
        dataset_names, tenancy_axis=tenancy_axis,
    ))
    envelope: Dict[str, Any] = {
        "mpf_version": MPF_VERSION,
        "source_system": SOURCE_SYSTEM,
        "source_version": _detect_cognee_version(),
        "source_instance": source_instance,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }
    if relations:
        envelope["relations"] = relations
    if kg_triples:
        envelope["kg_triples"] = kg_triples
    envelope["diagnostics"] = diag
    return envelope


# ─── Streaming iterator (for memory-efficient callers) ───────────────────────


def iter_records(
    dataset_names: Optional[List[str]] = None,
    *,
    tenancy_axis: str = "namespace",
) -> Iterator[Dict[str, Any]]:
    """Yield MPF records one at a time.

    Mirrors the mempalace adapter's streaming surface. Cognee itself
    returns the graph snapshot as a single ``(nodes, edges)`` pair, so
    we materialise once then yield — but callers get the familiar
    iterator interface for composition with downstream MPF tooling.
    """
    env = build_envelope(dataset_names, tenancy_axis=tenancy_axis)
    for rec in env.get("records") or []:
        yield rec


# ─── MPF → MNEMOS POST (optional) ────────────────────────────────────────────


def _post_to_mnemos(
    envelope: Dict[str, Any],
    endpoint: str,
    api_key: str,
    *,
    batch_size: int = 200,
) -> Dict[str, int]:
    """POST to MNEMOS /v1/import?preserve_owner=true in batches.

    Matches the mempalace/mem0/letta/graphiti adapters: each batch
    carries a trimmed envelope header (mpf_version, source_system,
    source_version, exported_at) and a window of records. Relations
    and kg_triples ride the final batch to guarantee edge endpoints
    already exist server-side when the KG links are applied.
    """
    records = envelope.get("records") or []
    totals = {"imported": 0, "skipped": 0, "failed": 0}
    base = endpoint.rstrip("/") + "/v1/import?preserve_owner=true"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    total_batches = max(1, (len(records) + batch_size - 1) // batch_size)
    for idx in range(total_batches):
        start = idx * batch_size
        chunk = {
            "mpf_version": envelope["mpf_version"],
            "source_system": envelope.get("source_system"),
            "source_version": envelope.get("source_version"),
            "exported_at": envelope["exported_at"],
            "records": records[start:start + batch_size],
        }
        if idx == total_batches - 1:
            if envelope.get("relations"):
                chunk["relations"] = envelope["relations"]
            if envelope.get("kg_triples"):
                chunk["kg_triples"] = envelope["kg_triples"]
        data = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(base, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read())
                for k in ("imported", "skipped", "failed"):
                    totals[k] += int(body.get(k, 0))
                print(
                    f"  batch {idx + 1}/{total_batches}: "
                    f"imported={body.get('imported')} "
                    f"skipped={body.get('skipped')} "
                    f"failed={body.get('failed')}",
                    file=sys.stderr,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"  WARNING /v1/import HTTP {e.code}: {body}", file=sys.stderr)
            totals["failed"] += len(chunk["records"])
    return totals


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemos.tools.adapters.cognee",
        description=(
            "Cognee → MPF v0.1 adapter (CHARON). Reads a Cognee "
            "deployment via its Python API (graph + vector + relational "
            "doc catalogue) and emits an MPF envelope. Optionally POSTs "
            "it to a MNEMOS /v1/import endpoint."
        ),
    )
    p.add_argument("--dataset", action="append", dest="datasets", default=None,
                   metavar="NAME",
                   help="Restrict export to this dataset (by name). Repeatable. "
                        "Omit to export every dataset readable by the current user.")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Write MPF envelope to this file ('-' for stdout). "
                        "Omit when --post is used.")
    p.add_argument("--post", default=None, metavar="URL",
                   help="POST the envelope to a MNEMOS /v1/import endpoint "
                        "(e.g. http://localhost:5002). Requires --api-key.")
    p.add_argument("--api-key", default=None,
                   help="Bearer token for MNEMOS auth (needed with --post).")
    p.add_argument("--tenancy-axis", choices=("owner_id", "namespace"),
                   default="namespace",
                   help="Which MNEMOS tenancy axis to write Cognee dataset/owner "
                        "scoping into (default: namespace).")
    p.add_argument("--source-instance", default=None,
                   help="Diagnostic label written into the envelope "
                        "(e.g. a deployment hostname).")
    args = p.parse_args(argv)

    if not (args.out or args.post):
        print("ERROR: pass --out PATH or --post URL", file=sys.stderr)
        return 2
    if args.post and not args.api_key:
        print("ERROR: --post requires --api-key", file=sys.stderr)
        return 2

    _require_cognee()

    t0 = time.time()
    envelope = build_envelope(
        args.datasets,
        source_instance=args.source_instance,
        tenancy_axis=args.tenancy_axis,
    )
    elapsed = time.time() - t0
    diag = envelope.get("diagnostics", {})
    print(
        f"read {envelope['record_count']} records from cognee in {elapsed:.1f}s "
        f"(datasets={diag.get('datasets', 0)}, docs={diag.get('documents', 0)}, "
        f"nodes={diag.get('graph_nodes', 0)}, edges={diag.get('graph_edges', 0)}, "
        f"relations={diag.get('relations', 0)}, kg_triples={diag.get('kg_triples', 0)})",
        file=sys.stderr,
    )

    if args.out:
        if args.out == "-":
            json.dump(envelope, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            Path(args.out).write_text(
                json.dumps(envelope, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"wrote envelope to {args.out}", file=sys.stderr)

    if args.post:
        totals = _post_to_mnemos(envelope, args.post, args.api_key)
        print(
            f"POST complete: "
            f"imported={totals['imported']} "
            f"skipped={totals['skipped']} "
            f"failed={totals['failed']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
