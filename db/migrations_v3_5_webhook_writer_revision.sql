-- migrations_v3_5_webhook_writer_revision.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 6) — mark webhook delivery rows by writer revision.
--
-- Old v3.5-dev webhook writers do not know this column. They either leave it
-- NULL through explicit test fixtures or receive the legacy default of 0 when
-- inserting after this migration. Current runtime code explicitly writes
-- NEW_CODE_WRITER_REVISION=1, allowing recovery to distinguish fresh pending
-- rows from lease-less rows that may still have an old writer POST in flight.
-- ---------------------------------------------------------------------------

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS writer_revision INTEGER DEFAULT 0;

COMMENT ON COLUMN webhook_deliveries.writer_revision IS
    'Webhook delivery writer revision: 0/NULL means legacy or unknown; 1 means current lease-aware writer.';
