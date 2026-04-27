-- migrations_v3_5_webhook_succeeded_terminal_trigger.sql
-- ---------------------------------------------------------------------------
-- v3.5 (slice 3 round 24) - make succeeded webhook ACK rows terminal.
--
-- The succeeded-chain unique index prevents two canonical success rows, but a
-- stale id-only writer can otherwise move the existing success row back to a
-- live retry status and remove it from that partial index. This trigger makes
-- status='succeeded' terminal at the database boundary while still allowing
-- audit-only updates such as response_body capture and lease cleanup.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION webhook_deliveries_enforce_succeeded_terminal()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'succeeded' AND NEW.status IS DISTINCT FROM 'succeeded' THEN
        RAISE EXCEPTION
            'webhook_deliveries: cannot transition status away from succeeded (id=%, attempted new status=%)',
            OLD.id,
            NEW.status
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS webhook_deliveries_succeeded_terminal ON webhook_deliveries;

CREATE TRIGGER webhook_deliveries_succeeded_terminal
    BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW
    EXECUTE FUNCTION webhook_deliveries_enforce_succeeded_terminal();
