"""Typed MNEMOS runtime settings.

Environment reads are centralized here. Runtime code should import
``get_settings()`` and use typed fields instead of calling ``os.getenv`` or
``os.environ`` directly.

Allowed exceptions to the ban are:
  * ``mnemos/installer/*``: the install wizard runs before package config exists.
  * ``tests/*``: test-specific process environment setup is intentional.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "backend": "postgres",
        "rate_limit_storage": "redis://localhost:6379/1",
        "workers": 1,
        "graeae_mode_default": "auto",
        "log_level": "INFO",
        "compression_workers": 4,
        "auth_enabled": True,
    },
    "edge": {
        "backend": "sqlite",
        "rate_limit_storage": "memory://",
        "workers": 1,
        "graeae_mode_default": "single",
        "log_level": "INFO",
        "compression_workers": 1,
        "auth_enabled": False,
    },
    "dev": {
        "backend": "sqlite",
        "rate_limit_storage": "memory://",
        "workers": 1,
        "graeae_mode_default": "auto",
        "log_level": "DEBUG",
        "compression_workers": 1,
        "loose_timeouts": True,
        "auth_enabled": False,
    },
}

PROFILE_ALIASES = {
    "personal": "edge",
}

_PROFILE_DEFAULT_TARGETS = {
    "backend": ("database", "backend"),
    "rate_limit_storage": ("rate_limit", "storage_uri"),
    "workers": ("server", "workers"),
    "graeae_mode_default": ("graeae", "mode_default"),
    "log_level": ("logging", "level"),
    "compression_workers": ("compression", "workers"),
    "loose_timeouts": ("runtime", "loose_timeouts"),
    "auth_enabled": ("auth", "enabled"),
}


def _config_model_config(*, env_prefix: str = "", extra: str = "ignore") -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        extra=extra,
        populate_by_name=True,
    )


class _DatabaseSettings(BaseSettings):
    model_config = _config_model_config(env_prefix="PG_")

    backend: str = Field(
        "auto",
        validation_alias=AliasChoices("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"),
    )
    dsn: str = Field(
        "",
        validation_alias=AliasChoices("MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN"),
    )
    url: str = Field(
        "",
        validation_alias=AliasChoices("MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL"),
    )
    sqlite_path: Path = Field(
        default_factory=lambda: (Path.home() / ".mnemos" / "mnemos.db"),
        validation_alias=AliasChoices("MNEMOS_SQLITE_PATH", "SQLITE_DB_PATH", "PG_SQLITE_PATH"),
    )
    host: str = "localhost"
    port: int = 5432
    database: str = "mnemos"
    user: str = "mnemos_user"
    password: str = ""
    pool_min_size: int = Field(5, validation_alias="PG_POOL_MIN")
    pool_max_size: int = Field(20, validation_alias="PG_POOL_MAX")

    @field_validator("sqlite_path", mode="before")
    @classmethod
    def _expand_sqlite_path(cls, raw: Any) -> Path:
        return Path(raw).expanduser()


class _GraeaeSettings(BaseSettings):
    model_config = _config_model_config(extra="allow")

    providers: dict[str, Any] = Field(default_factory=dict)
    mode_default: str = Field("auto", validation_alias="GRAEAE_MODE_DEFAULT")
    nats_fanout: bool = Field(False, validation_alias="MNEMOS_GRAEAE_NATS_FANOUT")
    providers_enabled: str = Field("together,groq,openai,anthropic", validation_alias="GRAEAE_PROVIDERS")
    consensus_mode: bool = Field(True, validation_alias="GRAEAE_CONSENSUS_MODE")
    consensus_quorum_size: int = Field(3, validation_alias="GRAEAE_CONSENSUS_QUORUM_SIZE")
    cache_enabled: bool = Field(True, validation_alias="GRAEAE_CACHE_ENABLED")
    cache_ttl_seconds: int = Field(3600, validation_alias="GRAEAE_CACHE_TTL_SECONDS")
    elo_registry: Path = Field(
        Path("/var/lib/mnemos/graeae_elo_weights.json"),
        validation_alias="GRAEAE_ELO_REGISTRY",
    )


class _ServerSettings(BaseSettings):
    model_config = _config_model_config()

    bind: str = Field("127.0.0.1", validation_alias=AliasChoices("MNEMOS_BIND", "MNEMOS_HOST"))
    port: int = Field(5002, validation_alias="MNEMOS_PORT")
    workers: int = Field(1, validation_alias="MNEMOS_WORKERS")
    base: str = Field("http://localhost:5002", validation_alias="MNEMOS_BASE")
    base_configured: bool = False
    api_key: str = Field("", validation_alias="MNEMOS_API_KEY")
    profile: str = Field("personal", validation_alias="MNEMOS_PROFILE")
    max_body_bytes: int = Field(5 * 1024 * 1024, validation_alias="MAX_BODY_BYTES")
    cors_origins: str = Field(
        "http://localhost,http://127.0.0.1,http://127.0.0.1:5002,http://localhost:5002",
        validation_alias="CORS_ORIGINS",
    )
    session_secret: str = Field("", validation_alias="MNEMOS_SESSION_SECRET")
    session_https_only: bool = Field(False, validation_alias="MNEMOS_SESSION_HTTPS_ONLY")
    redis_url: str = Field(
        "redis://localhost:6379",
        validation_alias=AliasChoices("MNEMOS_REDIS_URL", "REDIS_URL"),
    )


class _WebhookSettings(BaseSettings):
    model_config = _config_model_config()

    dns_timeout: float = Field(10.0, validation_alias="WEBHOOK_DNS_TIMEOUT")
    http_timeout: float = Field(10.0, validation_alias="WEBHOOK_HTTP_TIMEOUT")
    lease_seconds: int | None = Field(None, validation_alias="WEBHOOK_LEASE_SECONDS")
    shutdown_drain_seconds: float | None = Field(None, validation_alias="WEBHOOK_SHUTDOWN_DRAIN_SECONDS")
    finalize_buffer_seconds: float = Field(5.0, validation_alias="WEBHOOK_FINALIZE_BUFFER_SECONDS")
    response_body_max_bytes: int = Field(2048, validation_alias="WEBHOOK_RESPONSE_BODY_MAX_BYTES")
    post_header_cleanup_timeout_seconds: float = Field(
        5.0,
        validation_alias="WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS",
    )
    max_concurrent_sends: int = Field(64, validation_alias="WEBHOOK_MAX_CONCURRENT_SENDS")
    repair_burst_seconds: float = Field(60.0, validation_alias="WEBHOOK_REPAIR_BURST_SECONDS")
    repair_burst_interval: float = Field(5.0, validation_alias="WEBHOOK_REPAIR_BURST_INTERVAL")
    repair_periodic_interval: float = Field(300.0, validation_alias="WEBHOOK_REPAIR_PERIODIC_INTERVAL")
    allow_private_hosts: bool = Field(False, validation_alias="WEBHOOK_ALLOW_PRIVATE_HOSTS")

    @model_validator(mode="after")
    def _derive_lease_defaults(self) -> "_WebhookSettings":
        default_lease = max(90, int(self.dns_timeout + self.http_timeout + 30))
        if self.lease_seconds is None:
            self.lease_seconds = default_lease
        if self.shutdown_drain_seconds is None:
            self.shutdown_drain_seconds = float(self.lease_seconds)
        return self


class _ProviderSettings(BaseSettings):
    model_config = _config_model_config()

    openai_api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field("", validation_alias="GEMINI_API_KEY")
    xai_api_key: str = Field("", validation_alias="XAI_API_KEY")
    groq_api_key: str = Field("", validation_alias="GROQ_API_KEY")
    perplexity_api_key: str = Field("", validation_alias="PERPLEXITY_API_KEY")
    together_api_key: str = Field("", validation_alias="TOGETHER_API_KEY")
    nvidia_api_key: str = Field("", validation_alias="NVIDIA_API_KEY")
    keys_path: Path | None = Field(None, validation_alias="MNEMOS_KEYS_PATH")
    api_keys_file: Path = Field(
        default_factory=lambda: Path.home() / ".config" / "mnemos" / "api_keys.json",
        validation_alias="API_KEYS_FILE",
    )
    gpu_provider_host: str = Field("http://localhost", validation_alias="GPU_PROVIDER_HOST")
    gpu_provider_port: str = Field("8000", validation_alias="GPU_PROVIDER_PORT")
    gpu_provider_timeout: float = Field(30.0, validation_alias="GPU_PROVIDER_TIMEOUT")
    inference_embed_host: str = Field("http://localhost:11434", validation_alias="INFERENCE_EMBED_HOST")
    inference_embed_model: str = Field("nomic-embed-text", validation_alias="INFERENCE_EMBED_MODEL")
    inference_embed_timeout: float = Field(10.0, validation_alias="INFERENCE_EMBED_TIMEOUT")

    def api_key_for(self, provider: str) -> str:
        keys = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google_gemini": self.gemini_api_key,
            "gemini": self.gemini_api_key,
            "xai": self.xai_api_key,
            "groq": self.groq_api_key,
            "perplexity": self.perplexity_api_key,
            "together_ai": self.together_api_key,
            "together": self.together_api_key,
            "nvidia": self.nvidia_api_key,
        }
        return keys.get(provider, "")


class _MCPSettings(BaseSettings):
    model_config = _config_model_config()

    token: str = Field("", validation_alias="MNEMOS_MCP_TOKEN")
    tokens: str = Field("", validation_alias="MNEMOS_MCP_TOKENS")
    bind: str = Field("127.0.0.1", validation_alias="MNEMOS_MCP_BIND")


class _RateLimitSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(True, validation_alias="RATE_LIMIT_ENABLED")
    default: str = Field("300/minute", validation_alias="RATE_LIMIT_DEFAULT")
    storage_uri: str = Field(
        "memory://",
        validation_alias=AliasChoices("RATE_LIMIT_STORAGE_URI", "RATE_LIMIT_STORAGE", "storage"),
    )
    trust_proxy: bool = Field(False, validation_alias="RATE_LIMIT_TRUST_PROXY")
    per_minute: int = Field(60, validation_alias=AliasChoices("MNEMOS_RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_PER_MINUTE"))

    @property
    def storage(self) -> str:
        """Backward-compatible alias for older internal callers."""
        return self.storage_uri


class _ResilienceSettings(BaseSettings):
    model_config = _config_model_config()

    circuit_breaker_redis_prefix: str = Field(
        "mnemos:cb:",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_CIRCUIT_BREAKER_REDIS_PREFIX",
            "MNEMOS_CIRCUIT_BREAKER_REDIS_PREFIX",
        ),
    )
    rate_limiter_redis_prefix: str = Field(
        "mnemos:rl:",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_RATE_LIMITER_REDIS_PREFIX",
            "MNEMOS_RATE_LIMITER_REDIS_PREFIX",
        ),
    )
    concurrency_redis_prefix: str = Field(
        "mnemos:conc:",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_CONCURRENCY_REDIS_PREFIX",
            "MNEMOS_CONCURRENCY_REDIS_PREFIX",
        ),
    )
    fallback_warning: bool = Field(True, validation_alias="MNEMOS_RESILIENCE_FALLBACK_WARNING")


class _ObservabilitySettings(BaseSettings):
    model_config = _config_model_config()

    structured_logs: bool = Field(False, validation_alias="MNEMOS_STRUCTURED_LOGS")
    tracing_enabled: bool = Field(True, validation_alias="MNEMOS_TRACING_ENABLED")
    metrics_enabled: bool = Field(True, validation_alias="MNEMOS_METRICS_ENABLED")
    # When True, /metrics requires the same Bearer token as the rest of
    # the API. Default False matches the Prometheus convention of
    # network-scoping the scrape endpoint via ingress / firewall rather
    # than per-request auth. Operators in environments where the
    # /metrics endpoint is reachable from less-trusted networks (shared
    # cloud Prometheus, public-internet-routed clusters) flip this on.
    metrics_require_auth: bool = Field(
        False, validation_alias="MNEMOS_METRICS_REQUIRE_AUTH",
    )
    otel_service_name: str = Field("mnemos", validation_alias="OTEL_SERVICE_NAME")
    otel_exporter_otlp_endpoint: str = Field("", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")


class _CompressionSettings(BaseSettings):
    model_config = _config_model_config()

    workers: int = Field(1, validation_alias="MNEMOS_COMPRESSION_WORKERS")
    contest_enabled: bool = Field(True, validation_alias="MNEMOS_CONTEST_ENABLED")
    contest_min_content_length: int = Field(0, validation_alias="MNEMOS_CONTEST_MIN_CONTENT_LENGTH")
    contest_stale_threshold_secs: int = Field(600, validation_alias="MNEMOS_CONTEST_STALE_THRESHOLD_SECS")
    apollo_enabled: bool = Field(True, validation_alias="MNEMOS_APOLLO_ENABLED")
    apollo_llm_fallback_enabled: bool = Field(True, validation_alias="MNEMOS_APOLLO_LLM_FALLBACK_ENABLED")
    judge_enabled: bool = Field(False, validation_alias="MNEMOS_JUDGE_ENABLED")
    judge_model: str = Field("judge-default", validation_alias="MNEMOS_JUDGE_MODEL")
    judge_mode: str = Field("llm", validation_alias="MNEMOS_JUDGE_MODE")
    cross_encoder_model: str = Field(
        "cross-encoder/ms-marco-MiniLM-L-12-v2",
        validation_alias="MNEMOS_CROSS_ENCODER_MODEL",
    )

    @field_validator("contest_stale_threshold_secs", mode="before")
    @classmethod
    def _non_negative_stale_threshold(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return value if value >= 0 else 0


class _MorpheusSettings(BaseSettings):
    model_config = _config_model_config()

    cluster_threshold: float = Field(0.85, validation_alias="MNEMOS_MORPHEUS_CLUSTER_THRESHOLD")
    use_llm: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_USE_LLM")
    consolidate: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_CONSOLIDATE")
    extract: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_EXTRACT")
    extract_verify: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_EXTRACT_VERIFY")
    extract_min_chars: int = Field(200, validation_alias="MNEMOS_MORPHEUS_EXTRACT_MIN_CHARS")
    extract_min_confidence: float = Field(0.6, validation_alias="MNEMOS_MORPHEUS_EXTRACT_MIN_CONFIDENCE")
    extract_muse: str = Field("qwen3-7b", validation_alias="MNEMOS_MORPHEUS_EXTRACT_MUSE")
    extract_verifier: str = Field("openai", validation_alias="MNEMOS_MORPHEUS_EXTRACT_VERIFIER")


class _PersephoneSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_PERSEPHONE_ENABLED")
    archive_after_days: int = Field(180, validation_alias="MNEMOS_PERSEPHONE_ARCHIVE_AFTER_DAYS")
    batch_size: int = Field(100, validation_alias="MNEMOS_PERSEPHONE_BATCH_SIZE")
    check_interval_seconds: float = Field(
        3600.0,
        validation_alias="MNEMOS_PERSEPHONE_CHECK_INTERVAL_SECONDS",
    )
    namespace: str = Field("default", validation_alias="MNEMOS_PERSEPHONE_NAMESPACE")

    @field_validator("archive_after_days", "batch_size", mode="before")
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1

    @field_validator("check_interval_seconds", mode="before")
    @classmethod
    def _positive_interval(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 3600.0
        return value if value > 0 else 3600.0


class KronosSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_KRONOS_ENABLED")
    default_sensitivity: float = Field(2.5, validation_alias="MNEMOS_KRONOS_SENSITIVITY")
    default_lookback_hours: int = Field(168, validation_alias="MNEMOS_KRONOS_LOOKBACK_HOURS")
    default_baseline_days: int = Field(30, validation_alias="MNEMOS_KRONOS_BASELINE_DAYS")

    @field_validator("default_sensitivity", mode="before")
    @classmethod
    def _positive_sensitivity(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 2.5
        return value if value > 0 else 2.5

    @field_validator("default_lookback_hours", "default_baseline_days", mode="before")
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1


class PantheonSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_PANTHEON_ENABLED")
    consultation_cap: int = Field(
        50,
        validation_alias="MNEMOS_PANTHEON_CONSULTATION_CAP",
    )
    routing_window_minutes: int = Field(
        15,
        validation_alias="MNEMOS_PANTHEON_ROUTING_WINDOW_MINUTES",
    )
    policy_latency_weight: float = Field(
        0.40,
        validation_alias="MNEMOS_PANTHEON_POLICY_LATENCY_WEIGHT",
    )
    policy_error_weight: float = Field(
        0.40,
        validation_alias="MNEMOS_PANTHEON_POLICY_ERROR_WEIGHT",
    )
    policy_cost_weight: float = Field(
        0.20,
        validation_alias="MNEMOS_PANTHEON_POLICY_COST_WEIGHT",
    )
    default_quality_floor: float = Field(
        0.80,
        validation_alias="MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR",
    )
    default_max_cost_usd_per_mtok: float = Field(
        10.0,
        validation_alias=AliasChoices(
            "MNEMOS_PANTHEON_DEFAULT_MAX_COST",
            "MNEMOS_PANTHEON_DEFAULT_MAX_COST_USD_PER_MTOK",
        ),
    )

    @field_validator("consultation_cap", "routing_window_minutes", mode="before")
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1

    @field_validator("policy_latency_weight", "policy_error_weight", "policy_cost_weight", mode="before")
    @classmethod
    def _non_negative_weight(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return value if value >= 0.0 else 0.0


class _FederationNatsPeerSettings(BaseModel):
    name: str
    nats_url: str
    nats_token: str | None = None
    base_url: str | None = None
    auth_token: str | None = None
    namespace_filter: list[str] | None = None
    category_filter: list[str] | None = None
    subjects: list[str] = Field(default_factory=lambda: ["mnemos.memory.>"])


class _FederationSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_FEDERATION_ENABLED")
    peers: str = Field("", validation_alias="MNEMOS_FEDERATION_PEERS")
    nats_peers: list[_FederationNatsPeerSettings] = Field(
        default_factory=list,
        validation_alias="MNEMOS_FEDERATION_NATS_PEERS",
    )
    allow_insecure: bool = Field(False, validation_alias="FEDERATION_ALLOW_INSECURE")
    allow_private: bool = Field(False, validation_alias="FEDERATION_ALLOW_PRIVATE")
    # When set, federation NATS receivers join a JetStream queue group
    # under a SHARED durable consumer per (peer, subject) instead of
    # their default single-replica per-(peer, subject) durable. JetStream
    # load-balances messages across replicas in the same group; this is
    # the supported multi-replica deployment shape (Audit Finding 5).
    # Empty (default) preserves single-replica behavior with no
    # cross-replica coordination — flip to a non-empty group name only
    # after every replica is known to be on a build that understands it.
    nats_queue_group: str = Field("", validation_alias="MNEMOS_FEDERATION_NATS_QUEUE_GROUP")


class _OAuthSettings(BaseSettings):
    model_config = _config_model_config()

    trust_proxy: bool = Field(False, validation_alias="OAUTH_TRUST_PROXY")


class _AuthSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_AUTH_ENABLED")
    default_namespace: str = Field("default", validation_alias="MNEMOS_DEFAULT_NAMESPACE")
    personal_user_id: str = Field("default", validation_alias="MNEMOS_PERSONAL_USER_ID")


class _RuntimeSettings(BaseSettings):
    model_config = _config_model_config()

    worker_shutdown_cancel_seconds: float = Field(10.0, validation_alias="WORKER_SHUTDOWN_CANCEL_SECONDS")
    pool_acquire_timeout: float = Field(10.0, validation_alias="MNEMOS_POOL_ACQUIRE_TIMEOUT")
    loose_timeouts: bool = Field(False, validation_alias="MNEMOS_LOOSE_TIMEOUTS")


class _ToolSettings(BaseSettings):
    model_config = _config_model_config()

    knossos_wing_axis: str = Field("namespace", validation_alias="KNOSSOS_WING_AXIS")
    knossos_default_wing: str = Field("default", validation_alias="KNOSSOS_DEFAULT_WING")
    neo4j_user: str = Field("neo4j", validation_alias="NEO4J_USER")
    neo4j_password: str = Field("", validation_alias="NEO4J_PASSWORD")
    falkordb_password: str | None = Field(None, validation_alias="FALKORDB_PASSWORD")


class _LoggingSettings(BaseSettings):
    model_config = _config_model_config()

    level: str = Field("INFO", validation_alias="MNEMOS_LOG_LEVEL")
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    file: str = "/tmp/mnemos.log"
    max_bytes: int = 10_485_760
    backup_count: int = 5


class _NatsSettings(BaseSettings):
    model_config = _config_model_config()

    url: str | None = Field(None, validation_alias="MNEMOS_NATS_URL")
    token: str | None = Field(None, validation_alias="MNEMOS_NATS_TOKEN")
    node_name: str = Field("", validation_alias="MNEMOS_NODE_NAME")
    # When set, the webhook NATS trigger uses a SHARED durable consumer
    # joined via this queue group instead of per-node durables. JetStream
    # load-balances delivery so only one replica receives each nudge
    # (rather than every replica racing for the Postgres SKIP LOCKED
    # claim). Empty (default) preserves the per-node behavior — safe for
    # both single- and multi-replica deployments, just wasteful in the
    # multi-replica case. Flip to a non-empty group name only after all
    # replicas understand it. (Audit Finding 5.)
    webhook_queue_group: str = Field(
        "", validation_alias="MNEMOS_WEBHOOK_NATS_QUEUE_GROUP"
    )


class Settings(BaseSettings):
    model_config = _config_model_config()

    _explicit_fields: dict[str, set[str]] = PrivateAttr(default_factory=dict)

    database: _DatabaseSettings
    graeae: _GraeaeSettings
    server: _ServerSettings
    webhook: _WebhookSettings
    providers: _ProviderSettings
    mcp: _MCPSettings
    rate_limit: _RateLimitSettings
    resilience: _ResilienceSettings
    observability: _ObservabilitySettings
    compression: _CompressionSettings
    morpheus: _MorpheusSettings
    persephone: _PersephoneSettings
    kronos: KronosSettings
    pantheon: PantheonSettings
    federation: _FederationSettings
    oauth: _OAuthSettings
    auth: _AuthSettings
    runtime: _RuntimeSettings
    tools: _ToolSettings
    logging: _LoggingSettings
    nats: _NatsSettings

    @property
    def profile(self) -> str:
        return self.server.profile

    @property
    def log_level(self) -> str:
        return self.logging.level

    def explicit_fields(self, group: str) -> set[str]:
        return set(self._explicit_fields.get(group, set()))


_settings: Settings | None = None
PG_CONFIG: dict[str, Any] = {}
GRAEAE_CONFIG: dict[str, Any] = {}


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _build_settings()
        _sync_compat_exports(_settings)
    return _settings


def normalize_profile(raw_profile: str | None) -> str:
    """Return the canonical deployment profile name.

    ``personal`` was the v3.x single-user profile name. In v4 it is an alias
    for the all-in-one ``edge`` profile.
    """
    profile = (raw_profile or "personal").strip().lower()
    profile = PROFILE_ALIASES.get(profile, profile)
    if profile not in PROFILE_DEFAULTS:
        valid = ", ".join(PROFILE_DEFAULTS)
        raise ValueError(
            f"Unsupported MNEMOS profile {raw_profile!r}; expected one of: {valid}. "
            "Legacy profile 'personal' is now an alias for 'edge'."
        )
    return profile


def _build_settings() -> Settings:
    toml_config = _load_toml()
    server_toml = _toml_section(toml_config, "server")
    server = _ServerSettings(**server_toml)
    server.profile = normalize_profile(_profile_from_sources(toml_config, server_toml, server))
    server.base_configured = "MNEMOS_BASE" in os.environ or "base" in server_toml
    groups = {
        "database": _DatabaseSettings(**_toml_section(toml_config, "database")),
        "graeae": _GraeaeSettings(**_toml_section(toml_config, "graeae")),
        "server": server,
        "webhook": _WebhookSettings(**_toml_section(toml_config, "webhook")),
        "providers": _ProviderSettings(**_toml_section(toml_config, "providers")),
        "mcp": _MCPSettings(**_toml_section(toml_config, "mcp")),
        "rate_limit": _RateLimitSettings(**_toml_section(toml_config, "rate_limit")),
        "resilience": _ResilienceSettings(**_toml_section(toml_config, "resilience")),
        "observability": _ObservabilitySettings(**_toml_section(toml_config, "observability")),
        "compression": _CompressionSettings(**_toml_section(toml_config, "compression")),
        "morpheus": _MorpheusSettings(**_toml_section(toml_config, "morpheus")),
        "persephone": _PersephoneSettings(**_toml_section(toml_config, "persephone")),
        "kronos": KronosSettings(**_toml_section(toml_config, "kronos")),
        "pantheon": PantheonSettings(**_toml_section(toml_config, "pantheon")),
        "federation": _FederationSettings(**_toml_section(toml_config, "federation")),
        "oauth": _OAuthSettings(**_toml_section(toml_config, "oauth")),
        "auth": _AuthSettings(**_toml_section(toml_config, "auth")),
        "runtime": _RuntimeSettings(**_toml_section(toml_config, "runtime")),
        "tools": _ToolSettings(**_toml_section(toml_config, "tools")),
        "logging": _LoggingSettings(**_toml_section(toml_config, "logging")),
        "nats": _NatsSettings(**_toml_section(toml_config, "nats")),
    }
    settings = Settings(
        database=groups["database"],
        graeae=groups["graeae"],
        server=groups["server"],
        webhook=groups["webhook"],
        providers=groups["providers"],
        mcp=groups["mcp"],
        rate_limit=groups["rate_limit"],
        resilience=groups["resilience"],
        observability=groups["observability"],
        compression=groups["compression"],
        morpheus=groups["morpheus"],
        persephone=groups["persephone"],
        kronos=groups["kronos"],
        pantheon=groups["pantheon"],
        federation=groups["federation"],
        oauth=groups["oauth"],
        auth=groups["auth"],
        runtime=groups["runtime"],
        tools=groups["tools"],
        logging=groups["logging"],
        nats=groups["nats"],
    )
    settings._explicit_fields = {
        group_name: set(group.model_fields_set)
        for group_name, group in groups.items()
        if isinstance(group, BaseSettings)
    }
    _apply_profile_defaults(settings)
    return settings


def _profile_from_sources(
    toml_config: dict[str, Any],
    server_toml: dict[str, Any],
    server: _ServerSettings,
) -> str:
    override = os.environ.get("MNEMOS_PROFILE_OVERRIDE", "").strip()
    if override:
        return override
    if "profile" in server_toml:
        return str(server_toml["profile"])
    deployment_toml = _toml_section(toml_config, "deployment")
    if "profile" in deployment_toml:
        return str(deployment_toml["profile"])
    return server.profile


def _apply_profile_defaults(settings: Settings) -> None:
    profile_defaults = PROFILE_DEFAULTS[settings.profile]
    for profile_key, value in profile_defaults.items():
        target = _PROFILE_DEFAULT_TARGETS.get(profile_key)
        if target is None:
            continue
        group_name, field_name = target
        if field_name in settings.explicit_fields(group_name):
            continue
        setattr(getattr(settings, group_name), field_name, value)


def _load_toml() -> dict[str, Any]:
    for toml_path in _config_paths():
        if toml_path.exists():
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def _config_paths() -> list[Path]:
    paths: list[Path] = []
    configured_path = os.environ.get("MNEMOS_CONFIG_PATH", "").strip()
    if configured_path:
        paths.append(Path(configured_path).expanduser())
    paths.extend(
        [
            Path.cwd() / "config.toml",
            Path(__file__).resolve().parents[2] / "config.toml",
            Path("/etc/mnemos/config.toml"),
        ]
    )

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = path.resolve() if path.exists() else path
        if normalized not in seen:
            unique.append(path)
            seen.add(normalized)
    return unique


def _toml_section(toml_config: dict[str, Any], section: str) -> dict[str, Any]:
    value = toml_config.get(section, {})
    return value if isinstance(value, dict) else {}


def _sync_compat_exports(settings: Settings) -> None:
    PG_CONFIG.clear()
    PG_CONFIG.update(settings.database.model_dump(mode="python"))
    GRAEAE_CONFIG.clear()
    GRAEAE_CONFIG.update(settings.graeae.model_dump(mode="python"))


def reload_settings() -> Settings:
    """Rebuild the settings singleton after changing env/config inputs."""
    global _settings
    _settings = _build_settings()
    _sync_compat_exports(_settings)
    return _settings


def set_profile_override(profile_value: str) -> Settings:
    """Pin the active deployment profile and refresh settings.

    Centralised so the CLI doesn't have to write os.environ directly
    (the env-discipline lint allowlists writes only in this module).
    Sets BOTH MNEMOS_PROFILE_OVERRIDE (the takes-precedence override)
    and MNEMOS_PROFILE (the default-from-env path) so any subprocess
    spawned afterwards inherits the same selection.
    """
    os.environ["MNEMOS_PROFILE_OVERRIDE"] = profile_value
    os.environ["MNEMOS_PROFILE"] = profile_value
    return reload_settings()


def mcp_nats_raw_enabled() -> bool:
    """True if MNEMOS_MCP_NATS_RAW is set to a truthy value.

    Bypass for the JSON-summary path on NATS-backed MCP SSE streams —
    when set, the raw NATS message body is forwarded verbatim instead
    of being re-shaped into a {subject, summary} envelope.
    """
    return os.getenv("MNEMOS_MCP_NATS_RAW", "").strip().lower() in {"1", "true", "yes", "on"}


def _reset_settings_for_tests() -> None:
    """Clear the singleton and refresh compatibility dicts.

    This is intentionally not used by application code. It exists so tests can
    exercise environment/config-file overrides without process isolation.
    """
    reload_settings()


_sync_compat_exports(get_settings())
