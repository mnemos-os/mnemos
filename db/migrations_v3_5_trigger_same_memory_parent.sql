-- migrations_v3_5_trigger_same_memory_parent.sql
--
-- v3.5 (slice 2 round 39) — DB-level guard against corrupt
-- cross-memory parent_version_id edges in the version DAG.
--
-- Background: mnemos_version_snapshot's UPDATE branch resolves
-- parent_version_id via:
--
--   SELECT head_version_id INTO _parent_version
--   FROM memory_branches
--   WHERE memory_id = NEW.id AND name = _branch;
--
-- The schema only constrains memory_branches.head_version_id to an
-- existing memory_versions.id; it does NOT enforce that the
-- referenced version belongs to the SAME memory as the branch row.
-- Migration repair, partial imports, or admin pokes can leave a
-- corrupt branch row pointing at another memory's version_id.
-- Every ordinary PATCH/UPDATE on the parent memory then creates a
-- new version row whose parent_version_id points at a foreign
-- memory's version — a cross-memory DAG edge — and advances the
-- branch HEAD to the new same-memory row, hiding the corruption
-- while preserving the bad edge.
--
-- The HTTP/MCP handler-level fixes from rounds 36-38 close most of
-- the named paths, but ordinary updates still flow through the
-- trigger, so the same vulnerability remains live at the
-- highest-frequency write surface.
--
-- Fix: replace the trigger function. Resolve parent_version_id
-- through a scoped JOIN that requires the version's memory_id to
-- match the branch's memory_id. If the resolved head exists but
-- does NOT match (corrupt pointer), RAISE EXCEPTION with a
-- distinct SQLSTATE so the application layer can map it to a 409
-- + reconciliation message rather than silently writing a bad
-- parent edge.
--
-- The DELETE branch remains live for deployments that still have
-- trg_memory_version_delete attached. Keep it consistent with UPDATE:
-- scoped parent lookup, write a delete snapshot, and advance branch
-- HEAD to that tombstone version.
--
-- Idempotent: CREATE OR REPLACE FUNCTION rebinds the existing
-- trigger automatically since trigger definitions reference the
-- function by name, not by oid.

BEGIN;

CREATE OR REPLACE FUNCTION mnemos_version_snapshot() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    _next_v          INTEGER;
    _by              TEXT;
    _branch          TEXT;
    _commit_hash     TEXT;
    _parent_version  UUID;
    _new_version_id  UUID;
    _bare_head       UUID;
BEGIN
    _by := NULLIF(current_setting('mnemos.current_user_id', TRUE), '');
    _branch := COALESCE(NULLIF(current_setting('mnemos.current_branch', TRUE), ''), 'main');

    IF TG_OP = 'INSERT' THEN
        _commit_hash := encode(
            sha256(convert_to(NEW.id || '|1|' || NEW.content || '|' || NOW()::text, 'UTF8')),
            'hex'
        );

        INSERT INTO memory_versions (
            memory_id, version_num, content, category, subcategory, metadata,
            verbatim_content, owner_id, namespace, permission_mode,
            source_model, source_provider, source_session, source_agent,
            snapshot_by, change_type, commit_hash, branch, parent_version_id
        ) VALUES (
            NEW.id, 1, NEW.content, NEW.category, NEW.subcategory, NEW.metadata,
            NEW.verbatim_content, NEW.owner_id, NEW.namespace, NEW.permission_mode,
            NEW.source_model, NEW.source_provider, NEW.source_session, NEW.source_agent,
            _by, 'create', _commit_hash, _branch, NULL
        ) RETURNING id INTO _new_version_id;

        INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
        VALUES (NEW.id, _branch, _new_version_id, _by)
        ON CONFLICT (memory_id, name) DO UPDATE
        SET head_version_id = EXCLUDED.head_version_id;

    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.content         IS DISTINCT FROM NEW.content
        OR OLD.category        IS DISTINCT FROM NEW.category
        OR OLD.subcategory     IS DISTINCT FROM NEW.subcategory
        OR OLD.metadata        IS DISTINCT FROM NEW.metadata
        OR OLD.verbatim_content IS DISTINCT FROM NEW.verbatim_content
        OR OLD.permission_mode IS DISTINCT FROM NEW.permission_mode
        OR OLD.namespace       IS DISTINCT FROM NEW.namespace
        OR OLD.owner_id        IS DISTINCT FROM NEW.owner_id
        THEN
            SELECT COALESCE(MAX(version_num), 0) + 1
            INTO   _next_v
            FROM   memory_versions
            WHERE  memory_id = NEW.id AND branch = _branch;

            -- Scoped parent resolution. JOIN through memory_versions
            -- with mv.memory_id = mb.memory_id so a corrupt
            -- cross-memory head_version_id returns NULL here even
            -- when a bare memory_branches row exists.
            SELECT mb.head_version_id INTO _parent_version
            FROM memory_branches mb
            INNER JOIN memory_versions mv
                ON mv.id = mb.head_version_id
               AND mv.memory_id = mb.memory_id
            WHERE mb.memory_id = NEW.id AND mb.name = _branch;

            IF _parent_version IS NULL THEN
                -- Distinguish "no branch row" (legitimate first-write
                -- on a non-main branch via API the parent NULL is
                -- expected for v1) from "branch row exists but its
                -- head points outside this memory" (corruption).
                SELECT head_version_id INTO _bare_head
                FROM memory_branches
                WHERE memory_id = NEW.id AND name = _branch;
                IF _bare_head IS NOT NULL THEN
                    -- Distinct SQLSTATE 'MN001' so application code
                    -- can map this to HTTP 409 with reconciliation
                    -- guidance.
                    RAISE EXCEPTION
                        'mnemos: branch % for memory % has corrupt head_version_id (points outside this memory)',
                        _branch, NEW.id
                        USING ERRCODE = 'MN001';
                END IF;
                -- _bare_head IS NULL: no branch row at all. Falls
                -- through with _parent_version NULL, which is
                -- legitimate for the first version on a fresh
                -- branch (the trigger creates the row below via
                -- ON CONFLICT DO UPDATE on INSERT, but the UPDATE
                -- branch can also be reached if a memory exists
                -- without a branch row — caller should be using
                -- the explicit branch-creation path instead).
            END IF;

            _commit_hash := encode(
                sha256(convert_to(NEW.id || '|' || _next_v::text || '|' || NEW.content || '|' || NOW()::text, 'UTF8')),
                'hex'
            );

            INSERT INTO memory_versions (
                memory_id, version_num, content, category, subcategory, metadata,
                verbatim_content, owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                snapshot_by, change_type, commit_hash, branch, parent_version_id
            ) VALUES (
                NEW.id, _next_v,
                NEW.content, NEW.category, NEW.subcategory, NEW.metadata,
                NEW.verbatim_content, NEW.owner_id, NEW.namespace, NEW.permission_mode,
                NEW.source_model, NEW.source_provider, NEW.source_session, NEW.source_agent,
                _by, 'update', _commit_hash, _branch, _parent_version
            ) RETURNING id INTO _new_version_id;

            UPDATE memory_branches
            SET head_version_id = _new_version_id
            WHERE memory_id = NEW.id AND name = _branch;
        END IF;

    ELSIF TG_OP = 'DELETE' THEN
        -- DELETE is live when trg_memory_version_delete is attached.
        -- Write the tombstone snapshot and move branch HEAD to it,
        -- using the same scoped parent resolution as UPDATE.
        SELECT COALESCE(MAX(version_num), 0) + 1
        INTO   _next_v
        FROM   memory_versions
        WHERE  memory_id = OLD.id AND branch = _branch;

        SELECT mb.head_version_id INTO _parent_version
        FROM memory_branches mb
        INNER JOIN memory_versions mv
            ON mv.id = mb.head_version_id
           AND mv.memory_id = mb.memory_id
        WHERE mb.memory_id = OLD.id AND mb.name = _branch;

        IF _parent_version IS NULL THEN
            SELECT head_version_id INTO _bare_head
            FROM memory_branches
            WHERE memory_id = OLD.id AND name = _branch;
            IF _bare_head IS NOT NULL THEN
                RAISE EXCEPTION
                    'mnemos: branch % for memory % has corrupt head_version_id (points outside this memory)',
                    _branch, OLD.id
                    USING ERRCODE = 'MN001';
            END IF;
        END IF;

        _commit_hash := encode(
            sha256(convert_to(OLD.id || '|' || _next_v::text || '|' || OLD.content || '|' || NOW()::text, 'UTF8')),
            'hex'
        );

        INSERT INTO memory_versions (
            memory_id, version_num, content, category, subcategory, metadata,
            verbatim_content, owner_id, namespace, permission_mode,
            source_model, source_provider, source_session, source_agent,
            snapshot_by, change_type, commit_hash, branch, parent_version_id
        ) VALUES (
            OLD.id, _next_v,
            OLD.content, OLD.category, OLD.subcategory, OLD.metadata,
            OLD.verbatim_content, OLD.owner_id, OLD.namespace, OLD.permission_mode,
            OLD.source_model, OLD.source_provider, OLD.source_session, OLD.source_agent,
            _by, 'delete', _commit_hash, _branch, _parent_version
        ) RETURNING id INTO _new_version_id;

        UPDATE memory_branches
        SET head_version_id = _new_version_id
        WHERE memory_id = OLD.id AND name = _branch;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$;

COMMIT;
