-- SQLite mirror for db/migrations_v5_0_2_artemis_dedup.sql.
-- SQLite cannot add a STORED generated column via ALTER TABLE, so the
-- edge backend keeps a normal column maintained by API writes plus
-- triggers for direct SQL import paths.

ALTER TABLE memories
    ADD COLUMN content_hash TEXT;

ALTER TABLE memories
    ADD COLUMN deleted_at TEXT;

UPDATE memories
SET content_hash = mnemos_content_sha256(content)
WHERE content_hash IS NULL;

CREATE TRIGGER IF NOT EXISTS trg_memories_content_hash_insert
AFTER INSERT ON memories
FOR EACH ROW
WHEN NEW.content_hash IS NULL
BEGIN
    UPDATE memories
    SET content_hash = mnemos_content_sha256(NEW.content)
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_content_hash_update
AFTER UPDATE OF content ON memories
FOR EACH ROW
WHEN NEW.content_hash IS NULL
  OR NEW.content_hash <> mnemos_content_sha256(NEW.content)
BEGIN
    UPDATE memories
    SET content_hash = mnemos_content_sha256(NEW.content)
    WHERE id = NEW.id;
END;

CREATE INDEX IF NOT EXISTS idx_memories_owner_namespace_content_hash_active
    ON memories(owner_id, namespace, content_hash)
    WHERE deleted_at IS NULL
      AND archived_at IS NULL
      AND consolidated_into IS NULL
      AND content_hash IS NOT NULL;
