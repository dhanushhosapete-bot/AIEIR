-- Run before migrations, as the database owner. Works on managed Postgres (Render, RDS,
-- Cloud SQL...) where the owner is not a superuser but has CREATEROLE.
--
-- Two login roles, two connection pools:
--   aieir_app   serves founders. Sees only the current founder's rows. Can WRITE safety
--               events and alerts for that founder but can never READ any zone data.
--   aieir_staff serves the internal dashboard. Read-only on founder data, gated by a
--               staff role, and every sensitive read is audit-logged by the app.
-- Neither role may own tables, be a superuser, or have BYPASSRLS: any of those would
-- switch row-level security off for it. The final check refuses to continue otherwise.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aieir_app') THEN
    CREATE ROLE aieir_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aieir_staff') THEN
    CREATE ROLE aieir_staff LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    ALTER ROLE aieir_app NOSUPERUSER NOBYPASSRLS;
    ALTER ROLE aieir_staff NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('aieir_app', 'aieir_staff') AND (rolsuper OR rolbypassrls)) THEN
    RAISE EXCEPTION 'aieir_app and aieir_staff must not be superusers or have BYPASSRLS';
  END IF;
END $$;
-- Passwords are set by app/migrate.py from AIEIR_APP_PASSWORD / AIEIR_STAFF_PASSWORD.
