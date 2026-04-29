-- SQLite mirror for v3 federation tables.
-- There is no LISTEN/NOTIFY; federation pullers poll using compound cursors.
CREATE INDEX IF NOT EXISTS idx_federation_peers_cursor
  ON federation_peers(cursor_updated, cursor_id);
