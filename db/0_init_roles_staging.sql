-- 0_init_roles_staging.sql
--
-- Pre-init for staging deployments where the docker-compose creates
-- a single `mnemos_user` superuser, but later migrations (notably
-- migrations_model_registry.sql:75-76) GRANT to a `mnemos` role.
-- Production deployments create both roles via the installer flow
-- (`installer/db.py::_psql_superuser` runs as `postgres` and creates
-- both `mnemos` and `mnemos_user`); staging via docker-entrypoint-initdb
-- only gets POSTGRES_USER → mnemos_user, so we add `mnemos` here.
--
-- Pattern: `mnemos` is a NOLOGIN role used for GRANT-based ownership
-- semantics. `mnemos_user` is a member of `mnemos` and inherits all
-- privileges granted to it. App still connects as `mnemos_user`; any
-- migration that GRANTs to `mnemos` lands transparently.
--
-- This file is mounted as `/docker-entrypoint-initdb.d/00-init-roles.sql`
-- by docker-compose.staging.yml — sorts before `01-schema.sql` so the
-- `mnemos` role exists before any migration that references it.
--
-- Production deployments do NOT mount this file; they use the
-- installer's role-creation path which handles the same setup.

CREATE ROLE mnemos NOLOGIN;
GRANT mnemos TO mnemos_user;
