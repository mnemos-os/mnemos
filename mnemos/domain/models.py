"""Pydantic models for MNEMOS API."""
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, StrictInt


ConsultationMode = Literal["auto", "local", "external", "all", "single", "debate", "majority"]
SUPPORTED_CONSULTATION_MODES: tuple[str, ...] = (
    "auto",
    "local",
    "external",
    "all",
    "single",
    "debate",
    "majority",
)


class ConsultationRequest(BaseModel):
    prompt: str
    task_type: str = "reasoning"
    context: Optional[str] = None
    mode: ConsultationMode = "auto"
    limit_chars: Optional[int] = None
    format: Optional[str] = "full"

    # v3.2 Custom Query mode. Three optional selectors let the caller
    # pick their own muse lineup per query instead of getting the
    # default auto-resolved set. Precedence: models > providers > tier.
    # If none is set, behavior is unchanged (auto lineup).
    models: Optional[List[str]] = None
    providers: Optional[List[str]] = None
    tier: Optional[str] = None  # "frontier" | "premium" | "budget"


class StatsResponse(BaseModel):
    total_memories: int                              # native + federated
    native_memories: int = 0                         # locally-created (federation_source IS NULL)
    federated_memories: int = 0                      # pulled from peers (federation_source IS NOT NULL)
    memories_by_peer: Dict[str, int] = {}            # per-peer count of pulled memories
    total_compressions: int
    average_compression_ratio: float
    average_quality_rating: int
    memories_by_category: Dict[str, int]
    memories_by_subcategory: Dict[str, Dict[str, int]] = {}
    unreviewed_compressions: int
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    database_connected: bool
    version: str
    profile: str = "edge"
    distillation_worker: Optional[str] = None  # idle, healthy, error, disabled, unavailable
    nats_publishing_enabled: bool = False


class MemoryItem(BaseModel):
    id: str
    content: str
    category: str
    subcategory: Optional[str] = None
    created: str
    updated: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    quality_rating: Optional[int] = None
    compressed_content: Optional[str] = None
    verbatim_content: Optional[str] = None
    source: Optional[str] = "openclaw"
    # v1 provenance + ownership
    owner_id: Optional[str] = None
    group_id: Optional[str] = None
    namespace: Optional[str] = None
    permission_mode: Optional[StrictInt] = None
    source_model: Optional[str] = None
    source_provider: Optional[str] = None
    source_session: Optional[str] = None
    source_agent: Optional[str] = None
    archived_at: Optional[str] = None
    archived: bool = False


class FederationConsolidationEvent(BaseModel):
    type: Literal["consolidation"] = "consolidation"
    id: str
    consolidated_into: str
    consolidated_at: str


def _isoformat_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def row_to_memory(row, include_compressed: bool = False) -> MemoryItem:
    raw_meta = row.get("metadata")
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except Exception:
            raw_meta = None
    elif not isinstance(raw_meta, dict):
        raw_meta = None
    return MemoryItem(
        id=row["id"],
        content=row["content"],
        category=row["category"],
        subcategory=row.get("subcategory"),
        created=_isoformat_value(row["created"]) or "",
        updated=_isoformat_value(row.get("updated")),
        metadata=raw_meta if raw_meta else None,
        quality_rating=row.get("quality_rating"),
        compressed_content=row.get("compressed_content") if include_compressed else None,
        verbatim_content=row.get("verbatim_content"),
        owner_id=row.get("owner_id"),
        group_id=row.get("group_id"),
        namespace=row.get("namespace"),
        permission_mode=row.get("permission_mode"),
        source_model=row.get("source_model"),
        source_provider=row.get("source_provider"),
        source_session=row.get("source_session"),
        source_agent=row.get("source_agent"),
        archived_at=_isoformat_value(row.get("archived_at")),
        archived=row.get("archived_at") is not None,
    )


class MemoryListResponse(BaseModel):
    count: int
    memories: List[MemoryItem]
    compression_applied: Optional[bool] = False
    compression_metadata: Optional[Dict[str, Any]] = None


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 10
    category: Optional[str] = None
    subcategory: Optional[str] = None
    include_compressed: Optional[bool] = False
    semantic: Optional[bool] = False   # True = pgvector cosine similarity; False = FTS
    # Provenance filters — all optional, ANDed together when set
    source_provider: Optional[str] = None
    source_model: Optional[str] = None
    source_agent: Optional[str] = None
    namespace: Optional[str] = None
    include_archived: Optional[bool] = False
    boost_recency: Optional[bool] = False
    recency_weight: float = Field(0.15, ge=0.0, le=1.0)


class MemoryCreateRequest(BaseModel):
    content: str
    category: str = "facts"
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    verbatim_content: Optional[str] = None
    permission_mode: Optional[StrictInt] = None
    # v1 provenance + ownership (optional admin overrides; default to caller context)
    owner_id: Optional[str] = None
    namespace: Optional[str] = None
    source: Optional[str] = None  # source system/origin — e.g. "openclaw", "claude-code"
    source_model: Optional[str] = None  # e.g., "gpt-4o", "claude-3-5-sonnet"
    source_provider: Optional[str] = None  # e.g., "openai", "anthropic"
    source_session: Optional[str] = None  # session_id if created during session
    source_agent: Optional[str] = None  # agent name if created by autonomous agent


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    quality_rating: Optional[int] = None
    verbatim_content: Optional[str] = None  # original uncompressed content
    permission_mode: Optional[StrictInt] = None


class BulkCreateRequest(BaseModel):
    memories: List[MemoryCreateRequest]


class BulkCreateResponse(BaseModel):
    created: int
    memory_ids: List[str] = []  # IDs of successfully created memories
    errors: List[str] = []  # Per-item error messages


class RehydrationRequest(BaseModel):
    query: str
    limit: int = 5
    budget_tokens: Optional[int] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None


class RehydrationResponse(BaseModel):
    context: str
    tokens_used: int
    original_tokens: int
    compression_ratio: float
    quality_score: int
    memories_included: int
    compression_applied: bool


# ── Session Management Models (NEW) ────────────────────────────────────────

class ChatMessage(BaseModel):
    """Message in conversation history."""
    role: str  # "user", "assistant", "system"
    content: str
    timestamp: Optional[str] = None
    model: Optional[str] = None


class SessionContext(BaseModel):
    """Server-side context for a session."""
    session_id: str
    user_id: str
    created_at: str
    last_activity: str
    message_count: int
    total_tokens: int
    model: str
    injected_memories: Optional[List[str]] = None


class SessionRequest(BaseModel):
    """Create a new session."""
    model: Optional[str] = "gpt-4o"
    initial_context: Optional[str] = None


class SessionResponse(BaseModel):
    """Session created successfully."""
    session_id: str
    created_at: str
    model: str


class SessionMessage(BaseModel):
    """Add a message to a session (stateful chat)."""
    role: str  # "user" or "assistant"
    content: str
    model: Optional[str] = None  # Override session model


class SessionMessageResponse(BaseModel):
    """Response to a session message."""
    session_id: str
    message_id: str
    role: str
    content: str
    model: str
    timestamp: str
    tokens_used: int
    memories_injected: int


class SessionHistoryRequest(BaseModel):
    """Get session conversation history."""
    limit: int = 50
    offset: int = 0


class SessionHistoryResponse(BaseModel):
    """Session conversation history."""
    session_id: str
    messages: List[ChatMessage]
    total_messages: int
    total_tokens: int
    created_at: str


# ── Session Ingestion (Claude Code integration) ────────────────────────────────

class SessionIngestRequest(BaseModel):
    """Ingest session data from Claude Code."""
    raw_data: Dict[str, Any]
    session_id: str
    source: Optional[str] = "claude-code"
    machine_id: Optional[str] = None
    agent_id: Optional[str] = None
    git_commit: Optional[str] = None
    permission_mode: Optional[StrictInt] = None


class SessionIngestResponse(BaseModel):
    """Response from session ingestion."""
    success: bool
    session_id: str
    stored_count: int
    memory_ids: List[str]


# ── Knowledge Graph Models ────────────────────────────────────────────────────

class KGTriple(BaseModel):
    """Knowledge graph triple (subject → predicate → object)."""
    id: str
    subject: str
    predicate: str
    object: str
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_from: str
    valid_until: Optional[str] = None
    memory_id: Optional[str] = None
    confidence: float = 1.0
    created: str


class KGTripleCreate(BaseModel):
    """Create a knowledge graph triple."""
    subject: str
    predicate: str
    object: str
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    memory_id: Optional[str] = None
    confidence: float = 1.0


class KGTripleUpdate(BaseModel):
    """Update a knowledge graph triple."""
    object: Optional[str] = None
    confidence: Optional[float] = None
    valid_until: Optional[str] = None
    subject_type: Optional[str] = None
    object_type: Optional[str] = None
    predicate: Optional[str] = None
    subject: Optional[str] = None


class KGTripleListResponse(BaseModel):
    """List of knowledge graph triples."""
    count: int
    triples: List[KGTriple]


# ── Admin Models (User & API Key Management) ──────────────────────────────────

class UserCreateRequest(BaseModel):
    """Create a new user."""
    id: str
    display_name: str
    email: Optional[str] = None
    role: str = "user"  # "user" or "root"
    # Per-user tenancy namespace. v3.2 added users.namespace (2aa41ea)
    # and wired non-root reads to filter on it; the admin provisioning
    # API still silently defaulted every new user to 'default' which
    # collapsed multi-tenant installs. Operators now set this at
    # create-time.
    namespace: str = "default"


class UserResponse(BaseModel):
    """User response."""
    id: str
    display_name: str
    email: Optional[str] = None
    role: str
    namespace: str
    created_at: str


class ApiKeyCreateRequest(BaseModel):
    """Create an API key."""
    label: Optional[str] = None


class ApiKeyResponse(BaseModel):
    """API key response."""
    id: str
    user_id: str
    key_prefix: str
    label: Optional[str] = None
    created_at: str
    last_used: Optional[str] = None
    revoked: bool = False
    raw_key: Optional[str] = None  # only returned on creation


# ── v3.0.0 Consultations (GRAEAE Reasoning Domain) ────────────────────────────

class ConsultationResponse(BaseModel):
    """Response from GRAEAE consultation."""
    consultation_id: Optional[str] = None
    all_responses: Dict[str, Any]  # provider → response data
    consensus_response: Optional[str] = None
    consensus_score: Optional[float] = None
    winning_muse: Optional[str] = None
    cost: Optional[float] = None
    latency_ms: Optional[float] = None
    mode: str
    timestamp: str
    round_1: Optional[Dict[str, Any]] = None
    round_2: Optional[Dict[str, Any]] = None
    quorum_reached: Optional[bool] = None
    quorum_threshold: Optional[float] = None
    similarity_pairs: Optional[Dict[str, float]] = None


class ConsultationArtifact(BaseModel):
    """Structured output from consultation."""
    consultation_id: str
    citations: List[str]  # memory IDs referenced
    memory_refs: List[Dict[str, Any]]  # {memory_id, relevance_score, content}
    audit_hash: Optional[str] = None  # SHA-256 of prompt+response chain
    created_at: str


class ProviderResponse(BaseModel):
    """Single provider's response in consensus."""
    provider: str
    model_id: str
    status: str
    response_text: str
    latency_ms: float
    final_score: float
    quality_score: Optional[float] = None


class AuditLogEntry(BaseModel):
    """Hash-chained audit log entry."""
    id: str
    sequence_num: int
    consultation_id: Optional[str] = None
    prompt_hash: str
    response_hash: str
    chain_hash: Optional[str] = None
    prev_id: Optional[str] = None
    task_type: Optional[str] = None
    provider: Optional[str] = None
    quality_score: Optional[float] = None
    created_at: str


class AuditVerifyResponse(BaseModel):
    """Audit chain integrity verification."""
    valid: bool
    entries_checked: int
    first_broken_sequence: Optional[int] = None
    entries_failed: List[int] = Field(default_factory=list)
    message: str


# ── v3.0.0 Providers (Model Registry & Routing) ───────────────────────────────

class ProviderListResponse(BaseModel):
    """List of available LLM providers."""
    providers: List[str]
    total_models: int
    last_sync: Optional[str] = None


class ModelRecommendation(BaseModel):
    """Model recommendation for a task type."""
    recommended: Dict[str, Any]  # {provider, model_id, cost_per_mtok}
    reasoning: str
    quality_score: Optional[float] = None
    context_window: Optional[int] = None
    alternatives: Optional[List[Dict[str, Any]]] = None


# ── v3.0.0 Webhooks ───────────────────────────────────────────────────────────

VALID_WEBHOOK_EVENTS = frozenset({
    'memory.created',
    'memory.updated',
    'memory.deleted',
    'consultation.completed',
})


class WebhookCreateRequest(BaseModel):
    url: str
    events: List[str] = Field(..., description='Event types to subscribe to')
    description: Optional[str] = None
    namespace: Optional[str] = None


class WebhookItem(BaseModel):
    id: str
    url: str
    events: List[str]
    description: Optional[str] = None
    owner_id: str
    namespace: str
    created: str
    revoked: bool
    revoked_at: Optional[str] = None


class WebhookCreateResponse(BaseModel):
    id: str
    url: str
    events: List[str]
    description: Optional[str] = None
    owner_id: str
    namespace: str
    created: str
    revoked: bool
    secret: str = Field(
        ..., description='HMAC signing secret — shown once only, store securely'
    )


class WebhookListResponse(BaseModel):
    count: int
    webhooks: List[WebhookItem]


class WebhookDelivery(BaseModel):
    id: str
    subscription_id: str
    event_type: str
    attempt_num: int
    status: str
    superseded: bool = False
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None
    scheduled_at: str
    delivered_at: Optional[str] = None
    created: str


class WebhookDeliveryListResponse(BaseModel):
    count: int
    deliveries: List[WebhookDelivery]


# ── v3.0.0 OAuth / OIDC ───────────────────────────────────────────────────────

class OAuthProviderCreateRequest(BaseModel):
    name: str = Field(..., description="Unique provider name, e.g. 'google', 'github', 'company-sso'")
    display_name: str
    kind: str = Field("oidc", description="'oidc' | 'oauth2'")
    issuer_url: Optional[str] = Field(None, description="Required for kind='oidc'")
    client_id: str
    client_secret: str = Field(..., description="Stored in DB; rotate periodically")
    scope: str = "openid profile email"
    # oauth2-only overrides
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    userinfo_url: Optional[str] = None
    enabled: bool = True


class OAuthProviderUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    issuer_url: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scope: Optional[str] = None
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    userinfo_url: Optional[str] = None
    enabled: Optional[bool] = None


class OAuthProviderPublic(BaseModel):
    """Provider info safe to expose to unauthenticated login UI (no secrets)."""
    name: str
    display_name: str
    kind: str
    enabled: bool


class OAuthProviderAdmin(BaseModel):
    """Full provider record for admin UI (client_id visible, client_secret redacted)."""
    name: str
    display_name: str
    kind: str
    issuer_url: Optional[str] = None
    client_id: str
    client_secret_set: bool = Field(..., description="True if secret is set; raw value never returned")
    scope: str
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    userinfo_url: Optional[str] = None
    enabled: bool
    created: str
    updated: str


class OAuthProviderListResponse(BaseModel):
    count: int
    providers: List[OAuthProviderPublic]


class OAuthProviderAdminListResponse(BaseModel):
    count: int
    providers: List[OAuthProviderAdmin]


class OAuthIdentity(BaseModel):
    id: str
    user_id: str
    provider: str
    external_id: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    last_login_at: Optional[str] = None
    created: str


class OAuthIdentityListResponse(BaseModel):
    count: int
    identities: List[OAuthIdentity]


class OAuthLogoutResponse(BaseModel):
    logged_out: bool
    sessions_revoked: int


class OAuthMeResponse(BaseModel):
    """Who am I — useful for web UIs after redirect-callback."""
    user_id: str
    role: str
    namespace: str
    authenticated: bool
    auth_method: str      # 'api_key' | 'session' | 'personal'
    identity: Optional[OAuthIdentity] = None


# ── v3.0.0 Federation ─────────────────────────────────────────────────────────

class FederationPeerCreateRequest(BaseModel):
    name: str = Field(
        ...,
        description="Peer name (lowercase alnum + dash, 3-64 chars). Used in federated memory ids.",
    )
    base_url: str = Field(..., description="Peer base URL, e.g. https://peer.example.com")
    auth_token: str = Field(..., description="Bearer token the peer issued us (role=federation)")
    namespace_filter: Optional[List[str]] = None
    category_filter: Optional[List[str]] = None
    enabled: bool = True
    sync_interval_secs: int = 300
    compat_mode: str = Field(
        "strict",
        description=(
            "Schema-compat policy. 'strict' (default) refuses sync when peer's "
            "major.minor MNEMOS version differs from ours; 'permissive' logs the "
            "mismatch and proceeds. Use permissive only for known-compatible "
            "version skew across a federation."
        ),
    )


class FederationPeerUpdateRequest(BaseModel):
    base_url: Optional[str] = None
    auth_token: Optional[str] = None
    namespace_filter: Optional[List[str]] = None
    category_filter: Optional[List[str]] = None
    enabled: Optional[bool] = None
    sync_interval_secs: Optional[int] = None
    compat_mode: Optional[str] = None


class FederationPeer(BaseModel):
    id: str
    name: str
    base_url: str
    namespace_filter: Optional[List[str]] = None
    category_filter: Optional[List[str]] = None
    enabled: bool
    sync_interval_secs: int
    last_sync_at: Optional[str] = None
    last_sync_cursor: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    total_pulled: int = 0
    compat_mode: str = "strict"
    peer_mnemos_version: Optional[str] = None
    last_schema_check_at: Optional[str] = None
    created: str
    updated: str


class FederationPeerListResponse(BaseModel):
    count: int
    peers: List[FederationPeer]


class FederationSyncTriggerResponse(BaseModel):
    pulled: int
    new: int
    updated: int


class FederationSyncLogEntry(BaseModel):
    id: str
    started_at: str
    finished_at: Optional[str] = None
    memories_pulled: int
    memories_new: int
    memories_updated: int
    error: Optional[str] = None
    cursor_before: Optional[str] = None
    cursor_after: Optional[str] = None


class FederationSyncLogResponse(BaseModel):
    count: int
    entries: List[FederationSyncLogEntry]


class FederationStatusResponse(BaseModel):
    count: int
    enabled_count: int
    error_count: int
    peers: List[FederationPeer]


class FederationFeedResponse(BaseModel):
    """Returned by /v1/federation/feed to remote peers pulling from us."""
    memories: List[MemoryItem | FederationConsolidationEvent]
    next_cursor: Optional[str] = None
    has_more: bool = False


# ── GDPR right-to-be-forgotten (v4.2.0a14 round-77) ───────────


class DeletionRequestCreate(BaseModel):
    """Create a GDPR right-to-be-forgotten request.

    The request is recorded in ``deletion_requests``; the actual
    soft-delete sweep is performed by the wipe worker once the
    request transitions to ``confirmed`` (via the
    ``/admin/deletion-requests/{id}/confirm`` endpoint).

    ``target_namespace`` is optional. When NULL, the wipe covers
    ALL of the user's namespaces — the standard GDPR shape.
    Set explicitly to a namespace string to scope the wipe to a
    single tenant view of the user.
    """
    target_user_id: str
    target_namespace: Optional[str] = None
    notes: Optional[str] = None


class DeletionRequestItem(BaseModel):
    """A row from ``deletion_requests`` returned by admin
    endpoints (POST /admin/deletion-requests, GET, list)."""
    id: str
    target_user_id: str
    target_namespace: Optional[str] = None
    requested_by: str
    requested_at: str
    confirmed_at: Optional[str] = None
    soft_deleted_at: Optional[str] = None
    restore_by: Optional[str] = None
    restored_at: Optional[str] = None
    hard_deleted_at: Optional[str] = None
    status: str
    notes: Optional[str] = None


class DeletionRequestListResponse(BaseModel):
    count: int
    requests: List[DeletionRequestItem]
