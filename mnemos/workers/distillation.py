#!/usr/bin/env python3
"""
Background distillation worker: compresses memories using ARTEMIS (extractive,
identifier-preserving) or LLM fallback, updates embeddings, and maintains
compression quality metrics.

Lifecycle supervision lives in `api/lifecycle.py::_run_distillation_worker` —
this class knows how to do the work; that wrapper knows how to keep it alive
(exponential-backoff restart, capped at 5 min). See EVOLUTION.md ADR-02 for
the rationale behind the two-file separation.
"""

import asyncio
import logging
import os
import sys

import asyncpg

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Config
# Config — loaded from config.py (single source of truth)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from mnemos.core.config import PG_CONFIG as _PG_CONFIG, get_settings  # noqa: E402
from mnemos.core.extras import is_extra_installed  # noqa: E402

# Contest path: drains memory_compression_queue via the plugin
# CompressionEngine ABC + run_contest + persist_contest.
try:
    from mnemos.domain.compression.judge import CrossEncoderJudge, EnsembleJudge, LLMJudge, NullJudge
    from mnemos.domain.compression.worker_contest import process_contest_queue
    _CONTEST_AVAILABLE = True
except Exception as _ce:
    logger.warning(f"compression contest path unavailable: {_ce}")
    _CONTEST_AVAILABLE = False

_COMPRESSION_SETTINGS = get_settings().compression
_CONTEST_ENABLED = _COMPRESSION_SETTINGS.contest_enabled

# The current built-in compression stack is APOLLO + ARTEMIS.

# Optional minimum-content-length gate for the compression contest path.
# Memories shorter than this value are marked 'failed' with
# error='too_short' before any engine runs. Default 0 = no gate.
# Recommended 500 for GPU-constrained installs.
_CONTEST_MIN_CONTENT_LENGTH = _COMPRESSION_SETTINGS.contest_min_content_length

# APOLLO joined the default contest in v3.3 S-II. The engine is
# GPU_OPTIONAL (schema fast path is pure regex; LLM fallback uses
# the GPU host when reachable, short-circuits on a closed circuit,
# returns error on parse failure — see compression/apollo.py). The
# env var lets operators disable APOLLO entirely without editing code.
_APOLLO_ENABLED = _COMPRESSION_SETTINGS.apollo_enabled

# When APOLLO is on but the LLM fallback is unwanted (operators who
# want only the pure schema fast path — no GPU calls from APOLLO),
# flip this off. supports() then falls back to the S-IC shape:
# APOLLO skips non-schema-matching memories entirely.
_APOLLO_LLM_FALLBACK_ENABLED = _COMPRESSION_SETTINGS.apollo_llm_fallback_enabled

# Judge-LLM fidelity scoring (v3.3 S-II). When enabled, every
# successful contest candidate gets its quality_score replaced by a
# judge-rated fidelity score against the original memory. This is
# what lets APOLLO's LLM fallback win on fact-shaped content when its
# dense encoding preserves meaning. Operators opt in per the judge's
# GPU cost profile. When on, the MNEMOS_JUDGE_MODEL env var stamps
# the judge's id onto every scored candidate (default 'judge-default').
_JUDGE_ENABLED = _COMPRESSION_SETTINGS.judge_enabled
_JUDGE_MODEL = _COMPRESSION_SETTINGS.judge_model

# Judge mode selects the scoring implementation:
#   llm        — LLMJudge only (v3.3 S-II default; reasoning + fidelity)
#   cross      — CrossEncoderJudge only (fast CPU-only, no reasoning)
#   ensemble   — LLMJudge primary + CrossEncoderJudge secondary; primary
#                authoritative, secondary captured on the manifest for
#                correlation telemetry over a corpus
# Cross / ensemble modes require the `full` optional extra
# (sentence-transformers). Ensemble is the benchmark-gathering mode —
# run it for a window, compare primary/secondary agreement, decide
# whether to eventually promote the cross-encoder to the fast path.
_JUDGE_MODE = _COMPRESSION_SETTINGS.judge_mode.lower()
_CROSS_ENCODER_MODEL = _COMPRESSION_SETTINGS.cross_encoder_model

# Stale-running sweep threshold (v3.1.1). Queue rows stuck in 'running'
# longer than this are reclaimed at the top of each batch — reset to
# 'pending' (if attempts < max) or marked 'failed' (if attempts >= max).
# Covers the rare case where a worker crashed after dequeue but before
# any terminal status was recorded (both the contest-transaction commit
# AND the fresh-connection fallback mark-failed failed — pool exhausted,
# SIGKILL, etc.). Default 600s is safe for typical runs that finish in
# seconds. Set to 0 to disable the sweep entirely.
_CONTEST_STALE_THRESHOLD_SECS = _COMPRESSION_SETTINGS.contest_stale_threshold_secs

# Tuning
BATCH_SIZE = 5
CHECK_INTERVAL = 30
MAX_ATTEMPTS = 3

# DB connection kwargs — never build a DSN string with the password embedded
_DB_CONNECT_ARGS = {
    "user":     _PG_CONFIG["user"],
    "password": _PG_CONFIG["password"],
    "database": _PG_CONFIG["database"],
    "host":     _PG_CONFIG["host"],
    "port":     _PG_CONFIG["port"],
}


class MemoryDistillationWorker:
    def __init__(self):
        self.db_pool = None   # asyncpg Pool — set in start()
        # Contest engines — populated in start() once config is loaded
        self._contest_engines = []
        # v3.3 S-II judge — populated alongside engines in start()
        self._judge = None

    @staticmethod
    async def _create_pool():
        """Create the worker-local asyncpg pool wrapped with the
        ``TimeoutPool`` proxy so direct ``self.db_pool.acquire()``
        call sites inherit the configured acquire timeout.

        Without the wrap, the worker pool's acquires default to
        asyncpg's "wait forever" behaviour. Under a wedged or
        exhausted worker pool the contest-queue loop's bare
        ``acquire()`` calls would hang indefinitely and strand
        compression work — corpus-review #6 explicitly listed this
        worker as a remaining gap when the lifecycle wrap shipped.
        """
        # Lazy import keeps the worker module free of a hard
        # dependency on the lifecycle wrap during test bootstrap.
        from mnemos.core.pool import wrap_pool_with_timeout

        raw_pool = await asyncpg.create_pool(
            min_size=1, max_size=3, command_timeout=60, **_DB_CONNECT_ARGS,
        )
        return wrap_pool_with_timeout(raw_pool)

    async def start(self):
        """Start background worker"""
        logger.info(f"Connecting to DB: {_PG_CONFIG['host']}:{_PG_CONFIG['port']}/{_PG_CONFIG['database']}")
        self.db_pool = await self._create_pool()

        # Construct contest engines if available. Each engine is
        # lazy about creating HTTP clients — construction itself is
        # cheap and doesn't touch the network, so we always build the
        # enabled set and let the gpu_guard handle endpoint
        # unavailability at runtime.
        if _CONTEST_AVAILABLE and _CONTEST_ENABLED:
            # Default contest stack: Artemis + Apollo.
            #   Artemis: CPU-only extractive with identifier preservation,
            #            structure-aware, TextRank + MMR selection.
            #   Apollo:  Schema-aware dense encoding (portfolio / decision
            #            / person / event) with LLM fallback on misses.
            # Built-in stack: ARTEMIS + optional APOLLO.
            if is_extra_installed("artemis"):
                from mnemos.domain.compression.artemis import ARTEMISEngine

                self._contest_engines.append(ARTEMISEngine())
            else:
                logger.info("ARTEMIS contest engine disabled (extra not installed)")
            if _APOLLO_ENABLED and is_extra_installed("apollo"):
                from mnemos.domain.compression.apollo import APOLLOEngine

                self._contest_engines.append(
                    APOLLOEngine(
                        enable_llm_fallback=_APOLLO_LLM_FALLBACK_ENABLED,
                    )
                )
            elif _APOLLO_ENABLED:
                logger.info("APOLLO contest engine disabled (extra not installed)")
            if not self._contest_engines:
                logger.info("compression contest path disabled (no installed engines)")
                return
            # Judge fidelity scoring (v3.3 S-II). Selected by
            # MNEMOS_JUDGE_MODE (default 'llm') when enabled.
            # NullJudge when disabled — keeps the contest using
            # engine-self-reported scores (pre-S-II behavior).
            if not _JUDGE_ENABLED:
                self._judge = NullJudge()
            elif _JUDGE_MODE == "cross":
                try:
                    self._judge = CrossEncoderJudge(_CROSS_ENCODER_MODEL)
                except ImportError as exc:
                    logger.warning(
                        "CrossEncoderJudge unavailable (%s); falling back "
                        "to LLMJudge. CrossEncoderJudge requires "
                        "sentence-transformers which is no longer a default "
                        "mnemos extra (it pulls torch). Install it "
                        "explicitly if you need this judge: "
                        "pip install sentence-transformers",
                        exc,
                    )
                    self._judge = LLMJudge(model_id=_JUDGE_MODEL)
            elif _JUDGE_MODE == "ensemble":
                primary = LLMJudge(model_id=_JUDGE_MODEL)
                try:
                    secondary = CrossEncoderJudge(_CROSS_ENCODER_MODEL)
                    self._judge = EnsembleJudge(
                        primary=primary, secondaries=[secondary],
                    )
                except ImportError as exc:
                    logger.warning(
                        "Ensemble mode requested but CrossEncoderJudge "
                        "unavailable (%s); falling back to LLMJudge-only. "
                        "Install sentence-transformers explicitly to "
                        "enable ensemble (no longer pulled by any default "
                        "mnemos extra).",
                        exc,
                    )
                    self._judge = primary
            else:  # default and 'llm'
                self._judge = LLMJudge(model_id=_JUDGE_MODEL)
            engine_ids = [e.id for e in self._contest_engines]
            logger.info(
                "[OK] contest path enabled (engines: %s) judge=%s",
                ", ".join(engine_ids), type(self._judge).__name__,
            )
            if _APOLLO_ENABLED and not _APOLLO_LLM_FALLBACK_ENABLED:
                logger.info(
                    "APOLLO registered with LLM fallback DISABLED — "
                    "engine runs only on schema-matching memories. "
                    "Flip MNEMOS_APOLLO_LLM_FALLBACK_ENABLED=true to "
                    "cover all memories."
                )
            if _APOLLO_ENABLED and _APOLLO_LLM_FALLBACK_ENABLED and not _JUDGE_ENABLED:
                # APOLLO's LLM fallback emits a deliberately-low self-reported
                # quality_score (compression/apollo.py: _FALLBACK_QUALITY_SCORE
                # = 0.55) so it can never win the contest without external
                # validation. Without judge mode that validation never fires
                # — meaning APOLLO spends GPU on every fallback candidate
                # for nothing. This is wasted hardware. Either disable
                # the fallback (MNEMOS_APOLLO_LLM_FALLBACK_ENABLED=false)
                # or enable the judge (MNEMOS_JUDGE_ENABLED=true). See
                # docs/benchmarks/compression-2026-04-23.md for the
                # judge's GPU cost profile.
                logger.warning(
                    "APOLLO LLM fallback ENABLED but judge mode is OFF — "
                    "APOLLO fallback candidates self-score 0.55 and cannot "
                    "win the quality floor without judge re-scoring. "
                    "Set MNEMOS_JUDGE_ENABLED=true to enable the judge or "
                    "MNEMOS_APOLLO_LLM_FALLBACK_ENABLED=false to skip the "
                    "fallback path entirely. As-configured the fallback "
                    "path burns GPU on candidates that cannot win."
                )
        else:
            logger.info(
                "compression contest path disabled (available=%s, enabled=%s)",
                _CONTEST_AVAILABLE, _CONTEST_ENABLED,
            )

        logger.info("[OK] Distillation worker started")
        logger.info(f"Config: batch={BATCH_SIZE}, interval={CHECK_INTERVAL}s")

        while True:
            try:
                await self.process_contest_queue_batch()
                await self.log_stats()
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)
                try:
                    await self.db_pool.close()
                    self.db_pool = await self._create_pool()
                except Exception as re:
                    logger.error(f"DB reconnect failed: {re}")

            await asyncio.sleep(CHECK_INTERVAL)

    async def process_contest_queue_batch(self):
        """Drain up to BATCH_SIZE rows from memory_compression_queue via
        the contest path. No-op if contest engines aren't configured.

        Content / contest failures are swallowed and the next loop
        iteration retries. Infrastructure errors (pool exhaustion,
        asyncpg disconnect, asyncio.TimeoutError from the
        TimeoutPool wrap) are RE-RAISED so the worker loop's
        reconnect path can replace the wedged pool — codex round-2
        of round-28 caught that swallowing here masked acquire
        timeouts and kept a broken pool alive indefinitely.
        """
        from mnemos.core.pool import is_infrastructure_error

        if not self._contest_engines:
            return
        try:
            counts = await process_contest_queue(
                self.db_pool,
                self._contest_engines,
                batch_size=BATCH_SIZE,
                max_attempts=MAX_ATTEMPTS,
                min_content_length=_CONTEST_MIN_CONTENT_LENGTH,
                stale_threshold_secs=_CONTEST_STALE_THRESHOLD_SECS,
                judge=self._judge,
                judge_model=_JUDGE_MODEL if _JUDGE_ENABLED else None,
            )
            if counts:
                logger.info("contest queue drain: %s", counts)
        except Exception as e:
            if is_infrastructure_error(e):
                logger.warning(
                    "contest queue drain infrastructure error %s; "
                    "propagating to worker loop for reconnect",
                    type(e).__name__,
                )
                raise
            logger.error("contest queue drain error: %s", e, exc_info=True)

    async def log_stats(self):
        """Log current progress.

        Same infra-vs-content split as process_contest_queue_batch:
        infrastructure errors propagate so the worker loop reconnects;
        anything else is debug-logged and ignored (stats are best-effort
        — we don't want a transient stats query to take down the worker).
        """
        from mnemos.core.pool import is_infrastructure_error

        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        COUNT(*) AS total,
                        COUNT(CASE WHEN status = 'pending' THEN 1 END) AS pending,
                        COUNT(CASE WHEN status = 'running' THEN 1 END) AS running,
                        COUNT(CASE WHEN status = 'done' THEN 1 END) AS done,
                        COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed
                    FROM memory_compression_queue
                """)
                variants = await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_compressed_variants"
                )
            logger.info(
                "Compression queue: total=%s pending=%s running=%s done=%s "
                "failed=%s variants=%s",
                row["total"], row["pending"], row["running"], row["done"],
                row["failed"], variants,
            )
        except Exception as e:
            if is_infrastructure_error(e):
                logger.warning(
                    "log_stats infrastructure error %s; propagating to "
                    "worker loop for reconnect",
                    type(e).__name__,
                )
                raise
            logger.debug(f"Could not log stats: {e}")


async def main():
    worker = MemoryDistillationWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Worker shutting down gracefully")
    finally:
        if worker.db_pool:
            await worker.db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
