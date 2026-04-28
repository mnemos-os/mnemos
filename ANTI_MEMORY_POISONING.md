# Anti-Memory Poisoning Strategy in MNEMOS-OS

## Problem Statement

Memory poisoning occurs when infrastructure changes (API failures, model updates, system configuration changes) cause stored memories to become stale or contradictory. Without safeguards, an agent might act on outdated information that contradicts the current system state.

**Examples:**
- API endpoint changes from `/v1/auth` to `/v2/auth`, but memory still references old endpoint
- Model renamed from `gpt-4` to `gpt-4o`, but memory caches the old name
- Infrastructure IP changes from `192.168.1.1` to `192.168.1.2`, memory has stale reference

## MNEMOS-OS Solution: Git-Like DAG with Versioning

MNEMOS-OS implements anti-memory poisoning through **immutable, versioned memory history** with content-addressed commits (Phase 3: DAG Versioning).

### Key Mechanisms

#### 1. Content-Addressed Commits (Immutable)
```
commit_hash = SHA256(memory_id | version_num | content | snapshot_at)
```
- Deterministic: same content always produces same hash
- Immutable: hash cannot change without creating new version
- Auditable: can verify data integrity at any point in history

#### 2. Parent Pointers (Causality Chain)
```
version N → parent_version_id (points to N-1)
version N-1 → parent_version_id (points to N-2)
... → NULL (genesis commit)
```
- Unbroken chain proves causality
- Detect divergences (branching) by checking multiple children
- Reconstruct exact sequence of changes

#### 3. Branch Independence (Isolation)
```
main branch:     v1 → v2 → v3 (stable)
feature branch:  v1 → v2' → v3' (experimental)
bugfix branch:   v1 → v2'' (hot fix)
```
- Feature/bugfix branches don't affect main
- Merge only when confident (manual review or automated testing)
- Revert any branch to any past commit

#### 4. Change Type Tracking
```
version.change_type ∈ { "create", "update", "delete", "merge", "revert" }
```
- Know why a version was created
- Distinguish content edits from infrastructure changes
- Detect suspicious patterns (e.g., rapid delete/recreate)

#### 5. Audit Trail (Who, When, Why)
```
memory_versions:
  - snapshot_by: user_id who triggered the change
  - snapshot_at: precise timestamp (TIMESTAMPTZ)
  - metadata: {distillation_attempts, source_model, source_provider, ...}
session_memory_injections:
  - which memory was used in which session
  - relevance_score and injection timestamp
```

### Protection Against Common Scenarios

#### Scenario 1: API Endpoint Change
**Problem:** Memory contains old endpoint URL.
**Solution:**
- Create new memory entry with updated endpoint
- Old memory remains in history (readable via git log)
- Agents must explicitly query current version
- Diff shows when/what changed: `GET /v1/memories/{id}/diff?from=1&to=2`

#### Scenario 2: Model Rename
**Problem:** Memory references model that no longer exists.
**Solution:**
- Memory is immutable; old versions preserved
- New versions created with corrected model name
- Version log shows exact transition point: `GET /v1/memories/{id}/log`
- Agents can query timestamp-specific versions

#### Scenario 3: Configuration Drift
**Problem:** Infrastructure changed; memory contradicts current state.
**Solution:**
- Session memory injections are logged (session_memory_injections table)
- Each injection records memory_id, relevance_score, and timestamp; v3.5 removed
  the legacy session compression-ratio columns because they were not the real
  compression store
- Audit trail shows which memories were used and when
- Can trace decision back to specific memory version via commit_hash
- Revert to known-good configuration by checking out stable commit

#### Scenario 4: Poisoned Memory Detection
**Problem:** Detect that a memory has become stale/conflicting.
**Solution:**
- Use `log_memory` MCP tool: `get full history → identify where change happened`
- Use `diff_memory_commits`: compare v1 (stable) vs vN (current) to see drift
- Use `checkout_memory`: fetch exact commit content for verification
- Cross-reference session_memory_injections: "was this memory used in the failure session?"
- Merge strategically: don't blindly accept feature branch; review diff first

### Operational Guidelines

#### Creating Memory Entries
```python
# Mark source information for traceability
create_memory(
    content="API endpoint is https://api.example.com/v2/auth",
    category="infrastructure",
    metadata={
        "source_model": "gpt-4o",
        "source_provider": "openai",
        "verified_at": "2026-04-19T10:00:00Z",
        "verified_by": "human-review"  # or "automated-test"
    }
)
```

#### When Infrastructure Changes
```bash
# 1. Create new memory with updated information
curl -X POST http://localhost:5002/v1/memories \
  -d '{"content": "NEW: API endpoint is https://api.example.com/v3/auth", ...}'

# 2. Review history to understand drift
curl http://localhost:5002/v1/memories/{id}/log?branch=main

# 3. If memory is actively used, create bugfix branch
curl -X POST http://localhost:5002/v1/memories/{id}/branch \
  -d '{"name": "bugfix-api-v2-to-v3", "from_commit": "<hash>"}'

# 4. Make correction on bugfix branch
curl -X POST http://localhost:5002/v1/memories/{id}/revert/123 \
  -d '{"branch": "bugfix-api-v2-to-v3"}'

# 5. Merge back to main after validation
curl -X POST http://localhost:5002/v1/memories/{id}/merge \
  -d '{"source_branch": "bugfix-api-v2-to-v3", "strategy": "latest-wins"}'
```

#### Detecting Poisoning
```bash
# List all memories injected into a session
curl http://localhost:5002/v1/sessions/{id}/history

# Check if specific memory caused a problem
curl http://localhost:5002/v1/memories/{id}/log | jq '.commits[] | select(.snapshot_at > "2026-04-19T00:00:00")'

# Diff old vs new to spot drift
curl 'http://localhost:5002/v1/memories/{id}/diff?from=1&to=10'
```

### Best Practices

| Practice | Benefit |
|---|---|
| **Version strategically** | Create versions when infrastructure/config changes, not on every edit |
| **Use meaningful metadata** | source_model, source_provider, verified_by help trace decisions |
| **Branch for experiments** | Feature branches isolate speculative changes |
| **Review diffs before merge** | Never blindly accept branch merges |
| **Audit trail queries** | Regularly check session_memory_injections to correlate failures with memory usage |
| **Timestamp cross-reference** | "Did we use this memory at 14:00? The failure happened at 14:05." |

### Limitations & Tradeoffs

**What DAG Versioning Prevents:**
✓ Accidental loss of history (immutable commits)
✓ Unknown changes (audit trail, who/when/why)
✓ Divergence from current system state (branches + merge review)
✓ Silent corruption (content-addressed hashes)

**What It Does NOT Prevent:**
✗ Careless edits (humans can still write wrong information)
✗ Poisoned data from unreliable sources (garbage in → garbage out)
✗ Concurrency issues in agent decision-making (eventual consistency)

### Conclusion

MNEMOS-OS's DAG versioning + audit trail provides **infrastructure-level protection** against memory poisoning:
- **Immutability** prevents accidental/malicious overwrites
- **Causality chains** enable root-cause analysis
- **Branch isolation** allows safe experimentation
- **Audit trail** enables forensics and compliance

For maximum safety, combine with:
1. **Source verification** (mark who/what verified the memory)
2. **Change monitoring** (alerting on rapid changes to critical memories)
3. **Automated validation** (periodic cross-checks against current infrastructure)
4. **Agent review cycles** (don't blindly trust; validate against reality)
