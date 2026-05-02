-- migrations_v4_2_deletion_requests_sweep_verifying.sql
--
-- Round-86: add the transient ``sweep_verifying`` state used by the
-- deletion-request worker's second-pass verifier. The state remains
-- active for overlap/uniqueness purposes so an exhausted verify loop
-- leaves an operator-visible request that still blocks conflicting
-- wipes.

BEGIN;

ALTER TABLE deletion_requests
    DROP CONSTRAINT IF EXISTS deletion_requests_status_valid;

ALTER TABLE deletion_requests
    ADD CONSTRAINT deletion_requests_status_valid
    CHECK (status IN (
        'requested', 'confirmed', 'sweep_verifying', 'soft_deleted',
        'restored', 'hard_deleted', 'cancelled'
    ));

ALTER TABLE deletion_requests
    DROP CONSTRAINT IF EXISTS deletion_requests_status_lifecycle;

ALTER TABLE deletion_requests
    ADD CONSTRAINT deletion_requests_status_lifecycle CHECK (
        (status = 'requested' AND soft_deleted_at IS NULL AND hard_deleted_at IS NULL)
        OR (status = 'confirmed' AND confirmed_at IS NOT NULL AND soft_deleted_at IS NULL)
        OR (
            status = 'sweep_verifying'
            AND confirmed_at IS NOT NULL
            AND soft_deleted_at IS NULL
            AND hard_deleted_at IS NULL
        )
        OR (
            status = 'soft_deleted'
            AND soft_deleted_at IS NOT NULL
            AND restore_by IS NOT NULL
            AND hard_deleted_at IS NULL
        )
        OR (status = 'restored' AND restored_at IS NOT NULL)
        OR (status = 'hard_deleted' AND hard_deleted_at IS NOT NULL)
        OR (status = 'cancelled')
    );

DROP INDEX IF EXISTS deletion_requests_active_unique_idx;

CREATE UNIQUE INDEX deletion_requests_active_unique_idx
    ON deletion_requests (
        target_user_id,
        COALESCE(target_namespace, '*')
    )
    WHERE status IN ('requested', 'confirmed', 'sweep_verifying', 'soft_deleted');

COMMIT;
