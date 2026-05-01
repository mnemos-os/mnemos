-- migrations_v4_2_state_value_text.sql
--
-- Bring `state.value` into TEXT parity with SQLite. PG declared the
-- column JSONB in db/migrations.sql:135 but the
-- SqliteStateRepository (and the persistence-parity tests that
-- exercise both backends) treat values as opaque strings. Round-1
-- v4.2.0a5 attempted to bridge the gap with json.dumps + json.loads
-- in the repo, but that re-decodes any pre-existing JSON-string row
-- (e.g. legacy "42" stored as a JSONB scalar) into a different
-- Python type than what was originally written. Codex flagged this
-- as a silent semantic-change risk.
--
-- The principled fix is to make the on-disk shape match the SQLite
-- contract: TEXT, opaque, repo passes through. The HTTP /v1/state
-- route already wraps caller payloads in json.dumps before insert,
-- so it can keep its own JSON-on-the-wire envelope without help
-- from the column type.
--
-- Idempotent. ``USING value::text`` casts existing JSONB values to
-- their textual JSON representation; that preserves the bytes a
-- caller would have read back via the previous code path.
--
-- One caveat: the route's previous ``$4::jsonb`` cast on INSERT is
-- updated in the same commit to plain ``$4`` since the column no
-- longer enforces JSONB-shape validation. The route still wraps
-- with json.dumps so well-formed payloads remain queryable as JSON
-- via Python json.loads at the API edge.

BEGIN;

ALTER TABLE state
    ALTER COLUMN value TYPE TEXT USING value::text;

COMMIT;
