#!/usr/bin/env python3
"""MNEMOS Live End-to-End Test Suite v2"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("MNEMOS_BASE", "http://localhost:5000")
# Optional bearer token for auth-enabled deployments (team/bearer mode).
# Set MNEMOS_KEY env var; omit or leave empty for personal/no-auth mode.
API_KEY = os.getenv("MNEMOS_KEY", "")
PASS = FAIL = 0
created_ids = []

# Unique token per run — prevents stale data from previous runs affecting
# FTS search assertions in populated databases.
_TOKEN = f"mnemos_e2e_{random.randint(100000, 999999)}"

def req(method, path, body=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    hdrs = {"Content-Type": "application/json"}
    if API_KEY:
        hdrs["Authorization"] = f"Bearer {API_KEY}"
    r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            content = json.loads(raw) if raw.strip() else {}
            return status, content, None
    except urllib.error.HTTPError as e:
        try:
            content = json.loads(e.read())
        except Exception:
            content = {}
        return e.code, content, None
    except Exception as e:
        return None, None, str(e)

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name}" + (f": {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

# ─── 1. HEALTH & STATS ───────────────────────────────────────
section("1. Health & Stats")
st, r, _ = req("GET", "/health")
check("GET /health → 200", st == 200)
check("status=healthy", r and r.get("status") == "healthy")
check("database_connected=true", r and r.get("database_connected"))
check("timestamp in response", r and bool(r.get("timestamp")))

st, r, _ = req("GET", "/stats")
check("GET /stats → 200", st == 200)
check("total_memories > 0", r and r.get("total_memories", 0) > 0)
check("memories_by_category present", r and isinstance(r.get("memories_by_category"), dict))
print(f"    total_memories={r.get('total_memories')}, categories={len(r.get('memories_by_category',{}))}")

# ─── 2. MEMORY CRUD ──────────────────────────────────────────
section("2. Memory CRUD")

st, r, _ = req("POST", "/memories", {
    "content": f"MNEMOS live test: Python is a high-level interpreted language famous for readability. {_TOKEN}",
    "category": "system_tests",
    "metadata": {"test_run": "live_e2e"}
})
check("POST /memories → 201", st == 201)
check("id starts with mem_", r and r.get("id","").startswith("mem_"))
check("category persisted", r and r.get("category") == "system_tests")
check("created timestamp present", r and bool(r.get("created")))
mem_id_1 = r.get("id") if r else None
if mem_id_1:
    created_ids.append(mem_id_1)
print(f"    id={mem_id_1}")

st, r, _ = req("POST", "/memories", {
    "content": f"MNEMOS live test: pgvector enables similarity search for semantic memory retrieval. {_TOKEN}",
    "category": "system_tests",
})
check("POST /memories #2 → 201", st == 201)
mem_id_2 = r.get("id") if r else None
if mem_id_2:
    created_ids.append(mem_id_2)

st, r, _ = req("POST", "/memories", {
    "content": f"MNEMOS live test: the primary application host runs Linux with a documented hardware profile. {_TOKEN}",
    "category": "system_tests",
})
check("POST /memories #3 → 201", st == 201)
mem_id_3 = r.get("id") if r else None
if mem_id_3:
    created_ids.append(mem_id_3)

time.sleep(2)  # Let FTS settle

# GET single memory
st, r, _ = req("GET", f"/memories/{mem_id_1}")
check("GET /memories/{id} → 200", st == 200)
check("correct id returned", r and r.get("id") == mem_id_1)
check("content preserved", r and "Python" in r.get("content",""))

# GET nonexistent
st, r, _ = req("GET", "/memories/mem_nonexistent_000000")
check("GET nonexistent → 404", st == 404)

# ─── 3. LIST ─────────────────────────────────────────────────
section("3. Memory List & Pagination")

st, r, _ = req("GET", "/memories?limit=5")
check("GET /memories → 200", st == 200)
check("memories is a list", r and isinstance(r.get("memories"), list))
check("respects limit=5", r and len(r.get("memories",[])) <= 5)

st, r, _ = req("GET", "/memories?limit=3&offset=0")
first_page = r.get("memories",[]) if r else []
st, r, _ = req("GET", "/memories?limit=3&offset=3")
second_page = r.get("memories",[]) if r else []
first_ids = {m["id"] for m in first_page}
second_ids = {m["id"] for m in second_page}
check("Pagination: pages don't overlap", not first_ids.intersection(second_ids))

st, r, _ = req("GET", "/memories?category=system_tests&limit=10")
check("GET /memories?category filter → 200", st == 200)
test_mems = r.get("memories",[]) if r else []
check("≥3 system_tests memories visible", len(test_mems) >= 3, f"got {len(test_mems)}")

# ─── 4. SEARCH ───────────────────────────────────────────────
section("4. Memory Search")

# FTS: unique token + category filter guarantees the test memory ranks first
st, r, err = req("POST", "/memories/search", {
    "query": f"Python interpreted language readability {_TOKEN}",
    "category": "system_tests",
    "limit": 10
})
check("POST /memories/search → 200", st == 200, err)
results = r.get("memories",[]) if r else []
check("search returns results", len(results) > 0)
ids_returned = [m.get("id") for m in results]
check("Python memory in results", mem_id_1 in ids_returned,
      f"{mem_id_1} not in {ids_returned[:3]}")

# Category-filtered FTS
st, r, _ = req("POST", "/memories/search", {
    "query": f"pgvector similarity semantic retrieval {_TOKEN}",
    "category": "system_tests",
    "limit": 5
})
check("Category-filtered search → 200", st == 200)
filtered = r.get("memories",[]) if r else []
check("Category filter returns result", len(filtered) > 0, f"got {len(filtered)}")
check("pgvector memory in filtered results",
      mem_id_2 in [m.get("id") for m in filtered], f"ids: {[m.get('id') for m in filtered]}")

# Infrastructure query — category filter + token scopes to test data only
st, r, _ = req("POST", "/memories/search", {
    "query": f"primary application host Linux hardware profile {_TOKEN}",
    "category": "system_tests",
    "limit": 10
})
check("Infrastructure search → 200", st == 200)
check("Infrastructure memory found",
      mem_id_3 in [m.get("id") for m in r.get("memories",[])] if r else False)

# Nonsense → graceful empty
st, r, _ = req("POST", "/memories/search", {
    "query": "xyzzy_nonexistent_gibberish_9999_foobar",
    "limit": 3
})
check("Nonsense query → 200 (graceful)", st == 200)
check("Nonsense query returns 0 results", r and r.get("count",99) == 0)

# ─── 5. REHYDRATION ──────────────────────────────────────────
section("5. Rehydration")

st, r, _ = req("POST", "/memories/rehydrate", {
    "query": "Python language interpreted",
    "limit": 3,
    "budget_tokens": 1000
})
check("POST /memories/rehydrate → 200", st == 200)
check("context string returned", r and bool(r.get("context")))
check("tokens_used > 0", r and r.get("tokens_used",0) > 0)
check("memories_included > 0", r and r.get("memories_included",0) > 0)
check("compression_ratio in [0,2]", r and 0 <= r.get("compression_ratio",99) <= 2.0)
print(f"    tokens={r.get('tokens_used')}, memories={r.get('memories_included')}, ratio={r.get('compression_ratio')}")

st, r, _ = req("POST", "/memories/rehydrate", {
    "query": "infrastructure deployment",
    "limit": 10,
    "budget_tokens": 200
})
check("Rehydrate tight budget → 200", st == 200)
check("Budget respected (tokens_used ≤ 250)", r and r.get("tokens_used",999) <= 250,
      f"got {r.get('tokens_used') if r else 'n/a'}")

# Empty result graceful
st, r, _ = req("POST", "/memories/rehydrate", {
    "query": "xyzzy_gibberish_99999_foobar_nonexistent",
    "limit": 3, "budget_tokens": 500
})
check("Rehydrate no-match → 200 (graceful)", st == 200)
check("Empty rehydrate context is empty string", r and r.get("context","x") == "")

# ─── 6. GRAEAE CONSULT ───────────────────────────────────────
section("6. GRAEAE Consultation (all providers)")

t0 = time.time()
st, r, err = req("POST", "/graeae/consult", {
    "prompt": "In one sentence, what is a vector database used for?",
    "task_type": "reasoning",
    "mode": "best"
}, timeout=60)
elapsed = time.time() - t0

check("POST /graeae/consult → 200", st == 200, err or st)
check("all_responses present", r and "all_responses" in r)
providers = r.get("all_responses",{}) if r else {}
successes = [k for k,v in providers.items() if v.get("status") == "success"]
check("≥6 providers queried", len(providers) >= 6, f"got {len(providers)}")
check("≥4 providers succeeded", len(successes) >= 4, f"successes: {successes}")
best = max(providers.items(), key=lambda x: x[1].get("final_score",0), default=(None,{}))
check("best provider has response text", bool(best[1].get("response_text","")))
check("response is informative (>20 chars)", len(best[1].get("response_text","")) > 20)
print(f"    elapsed={elapsed:.1f}s | successes={successes}")
print(f"    best={best[0]} ({best[1].get('final_score',0):.2f}): {best[1].get('response_text','')[:80]}...")

st, r, _ = req("GET", "/graeae/health")
check("GET /graeae/health → 200", st == 200)

# ─── 7. EDGE CASES ───────────────────────────────────────────
section("7. Input Validation & Edge Cases")

# Empty/whitespace content
for label, content_val in [("empty string", ""), ("whitespace only", "   "), ("newlines only", "\n\n")]:
    st, r, _ = req("POST", "/memories", {"content": content_val, "category": "test"})
    check(f"Reject {label} (422)", st == 422, f"got {st}")

# Large payload — API accepts content of any size (no server-side cap)
long_content = "MNEMOS live test large content. " * 100  # ~3200 chars
st, r, _ = req("POST", "/memories", {"content": long_content, "category": "system_tests"})
check("Large content (3200 chars) accepted", st == 201)
if r and r.get("id"):
    created_ids.append(r["id"])

# ─── 8. DELETE ───────────────────────────────────────────────
section("8. DELETE endpoint")

# Create a throwaway memory
st, r, _ = req("POST", "/memories", {"content": "delete me please", "category": "system_tests"})
del_id = r.get("id") if r else None
check("Created memory for DELETE test", del_id is not None)

if del_id:
    st, r, _ = req("DELETE", f"/memories/{del_id}")
    check("DELETE /memories/{id} → 204", st == 204, f"got {st}: {r}")

    # Confirm it's gone
    st, r, _ = req("GET", f"/memories/{del_id}")
    check("GET after DELETE → 404", st == 404, f"got {st}")

# Delete nonexistent → 404
st, r, _ = req("DELETE", "/memories/mem_never_existed_abc123")
check("DELETE nonexistent → 404", st == 404, f"got {st}")


# --- 9b. SUBCATEGORY -------------------------------------------------------
section("9b. Subcategory Filtering")

st, r, _ = req("POST", "/memories", {
    "content": f"MNEMOS subcategory test: Kubernetes networking config. {_TOKEN}",
    "category": "infrastructure",
    "subcategory": "kubernetes",
})
check("POST /memories with subcategory -> 201", st == 201, f"got {st}")
check("subcategory persisted", r and r.get("subcategory") == "kubernetes")
sub_id = r.get("id") if r else None
if sub_id:
    created_ids.append(sub_id)

st, r, _ = req("GET", "/memories?category=infrastructure&subcategory=kubernetes&limit=5")
check("GET /memories?subcategory filter -> 200", st == 200, f"got {st}")
check("subcategory memory in list",
      sub_id in [m["id"] for m in (r or {}).get("memories", [])])

st, r, _ = req("POST", "/memories/search", {
    "query": f"Kubernetes networking config {_TOKEN}",
    "category": "infrastructure",
    "subcategory": "kubernetes",
    "limit": 5,
})
check("Search with subcategory filter -> 200", st == 200, f"got {st}")
check("subcategory-filtered search finds memory",
      sub_id in [m["id"] for m in (r or {}).get("memories", [])])

# --- 9c. KNOWLEDGE GRAPH ---------------------------------------------------
section("9c. Knowledge Graph")
kg_id = None

st, r, _ = req("POST", "/v1/kg/triples", {
    "subject": "primary-host",
    "predicate": "runs",
    "object": "MNEMOS",
    "subject_type": "server",
    "object_type": "service",
    "confidence": 0.99,
})
check("POST /kg/triples -> 201", st == 201, f"got {st}: {r}")
check("triple id starts with kg_", r and r.get("id", "").startswith("kg_"))
kg_id = r.get("id") if r else None

st, r, _ = req("GET", "/v1/kg/triples?subject=primary-host")
check("GET /kg/triples?subject -> 200", st == 200, f"got {st}")
check("created triple in list",
      r and any(t["id"] == kg_id for t in r.get("triples", [])))

st, r, _ = req("GET", "/v1/kg/timeline/primary-host")
check("GET /v1/kg/timeline/primary-host -> 200", st == 200, f"got {st}")
check("timeline returns triples", r and r.get("count", 0) > 0)

if kg_id:
    st, _, _ = req("DELETE", f"/v1/kg/triples/{kg_id}")
    check("DELETE /kg/triples/{id} -> 204", st == 204, f"got {st}")

check("DELETE nonexistent triple -> 404",
      req("DELETE", "/v1/kg/triples/kg_never_exists")[0] == 404)

# ─── 9. CLEANUP ──────────────────────────────────────────────
section("9. Cleanup (delete all test memories)")

deleted = 0
for mid in created_ids:
    st, _, _ = req("DELETE", f"/memories/{mid}")
    if st in (204, 404):
        deleted += 1
check(f"Cleaned {deleted}/{len(created_ids)} test memories",
      deleted == len(created_ids), f"only {deleted}/{len(created_ids)}")

# ─── SUMMARY ─────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  RESULTS: {PASS} passed, {FAIL} failed / {PASS+FAIL} total")
print('='*60)
sys.exit(0 if FAIL == 0 else 1)
