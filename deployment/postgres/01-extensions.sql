-- Runs once, when the Postgres data volume is first initialised.
-- Tables are created by Alembic migrations (Phase 6), not here.
CREATE EXTENSION IF NOT EXISTS vector;
