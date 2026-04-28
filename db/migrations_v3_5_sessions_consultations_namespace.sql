-- v3.5: namespace scoping for sessions and GRAEAE consultations.
-- Extends the v3.2 namespace tenancy model to remaining product surfaces.

BEGIN;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

ALTER TABLE graeae_consultations
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_sessions_owner_namespace
    ON sessions(user_id, namespace);

CREATE INDEX IF NOT EXISTS idx_graeae_consultations_owner_namespace
    ON graeae_consultations(owner_id, namespace);

COMMIT;
