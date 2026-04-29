"""Memory-version DAG validation and ordering."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mnemos.db import portability_repo as repo


async def _validate_version_parents(
    conn,
    parent_uuids: List[str],
    *,
    expected_record_id: str,
    effective_owner: str,
    effective_ns: Optional[str],
    in_envelope_index: Dict[str, Dict[str, Any]],
    preserve_owner: bool = True,
    require_in_envelope: bool = False,
    freshly_inserted_uuids: Optional[set] = None,
) -> tuple:
    if not parent_uuids:
        return True, []

    rows = await repo.fetch_memory_versions_by_ids(conn, parent_uuids)
    db_truth: Dict[str, tuple] = {
        r["id"]: (r["memory_id"], r["owner_id"], r["namespace"]) for r in rows
    }
    bad: List[str] = []
    for p in parent_uuids:
        if require_in_envelope:
            if freshly_inserted_uuids is None or p not in freshly_inserted_uuids:
                bad.append(p)
                continue
        if p in db_truth:
            mem_id, owner, ns = db_truth[p]
            if mem_id != expected_record_id or owner != effective_owner or ns != effective_ns:
                bad.append(p)
            continue
        if p in in_envelope_index:
            ref = in_envelope_index[p]
            ref_record = ref.get("record_id")
            same_record = ref_record == expected_record_id
            if preserve_owner:
                ref_owner = ref.get("owner_id")
                ref_ns = ref.get("namespace")
                same_owner = (ref_owner is None) or (ref_owner == effective_owner)
                same_ns = (ref_ns is None) or (ref_ns == effective_ns)
            else:
                same_owner = True
                same_ns = True
            if not (same_record and same_owner and same_ns):
                bad.append(p)
            continue
        bad.append(p)
    return (not bad), bad


def _topo_sort_versions(sidecar: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not sidecar:
        return []

    by_id: Dict[str, Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    for entry in sidecar:
        eid = entry.get("id")
        if eid:
            by_id[str(eid)] = entry
        else:
            no_id.append(entry)

    in_degree: Dict[str, int] = {eid: 0 for eid in by_id}
    children: Dict[str, List[str]] = {eid: [] for eid in by_id}
    for eid, entry in by_id.items():
        parents: List[str] = []
        pv = entry.get("parent_version_id")
        if pv:
            parents.append(str(pv))
        for mp in entry.get("merge_parents") or []:
            if mp:
                parents.append(str(mp))
        for p in parents:
            if p in by_id:
                in_degree[eid] += 1
                children[p].append(eid)

    def _key(eid: str) -> tuple:
        e = by_id[eid]
        return (int(e.get("version_num") or 0), eid)

    ready = sorted([eid for eid, d in in_degree.items() if d == 0], key=_key)
    out: List[Dict[str, Any]] = []
    while ready:
        eid = ready.pop(0)
        out.append(by_id[eid])
        for child in children[eid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                k = _key(child)
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if _key(ready[mid]) < k:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, child)

    if len(out) < len(by_id):
        leftover = sorted([eid for eid in by_id if by_id[eid] not in out], key=_key)
        out.extend(by_id[eid] for eid in leftover)

    return out + no_id
