-- Run once per cluster as a superuser, before migrations.
--
-- Two login roles, two connection pools:
--   aieir_app   serves founders. Sees only the current founder's rows. Can WRITE safety
--               events and alerts for that founder but can never READ any zone data.
--   aieir_staff serves the internal dashboard. Read-only on founder data, gated by a
--               staff role, and every sensitive read is audit-logged by the app.
-- Neither role may own tables, be a superuser, or have BYPASSRLS: any of those would
-- switch row-level security off for it.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aieir_app') THEN
    CREATE ROLE aieir_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aieir_staff') THEN
    CREATE ROLE aieir_staff LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
ALTER ROLE aieir_app NOSUPERUSER NOBYPASSRLS;
ALTER ROLE aieir_staff NOSUPERUSER NOBYPASSRLS;
-- Set passwords out of band, e.g.: ALTER ROLE aieir_app PASSWORD '...';
