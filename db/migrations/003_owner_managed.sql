-- Managed Postgres (Render and similar) gives the database owner no superuser rights.
-- With FORCE ROW LEVEL SECURITY the owner would also be bound by policies written only
-- for aieir_app / aieir_staff, which would block sign-up, account deletion and the
-- retention purge (all run as the owner through SECURITY DEFINER functions).
--
-- So the owner is exempt (Postgres's normal rule), and the two application roles stay
-- fully bound by RLS: they don't own any table, aren't superusers and lack BYPASSRLS,
-- which tests/test_privacy.py checks. The tests now run with a non-superuser owner, as
-- in production.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['startups', 'intake_interviews', 'roadmaps', 'weekly_kpis', 'sessions', 'reflections',
                           'messages', 'feedback', 'founder_state', 'users', 'zone_events', 'alerts', 'staff',
                           'reviews', 'audit_log']
  LOOP
    EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
