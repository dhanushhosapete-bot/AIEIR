-- AI EIR schema v1.
--
-- Privacy model
--   * Founder requests run as role aieir_app inside a transaction that sets app.user_id.
--     Row-level security limits every founder table to that founder's rows. If the
--     setting is missing, app_current_user() is NULL and policies match nothing.
--   * Staff requests run as role aieir_staff with app.staff_id / app.staff_role set from
--     an authenticated staff token. Staff can read founder data (to keep people safe) but
--     not change it, and only roles eir / safety_reviewer / admin exist.
--   * Safety zones are internal. aieir_app can insert zone events and alerts for the
--     current founder but has no SELECT policy on them, so no founder-facing code path
--     can read a zone label.
--   * Message, reflection, intake, rationale and review text is encrypted in the app
--     (AES-GCM, bound to the founder id) before it reaches these *_enc columns.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION app_current_user() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.user_id', true), '')::uuid
$$;
CREATE OR REPLACE FUNCTION app_staff_id() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.staff_id', true), '')::uuid
$$;
CREATE OR REPLACE FUNCTION app_staff_role() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN current_setting('app.staff_role', true) IN ('eir', 'safety_reviewer', 'admin')
              THEN current_setting('app.staff_role', true) END
$$;

-- ------------------------------------------------------------------ founders
CREATE TABLE users (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email            text NOT NULL UNIQUE,
  display_name     text,
  country          text NOT NULL DEFAULT 'US',
  consent_version  text NOT NULL,             -- safety-review consent shown at sign-up
  consented_at     timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auth_tokens (                   -- no RLS; reachable only via definer functions
  token_hash  text PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE startups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name text, one_liner text, sector text, stage text, goal text, goal_due date,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE intake_interviews (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'complete')),
  answers_enc   text,                          -- encrypted JSON list of {question_id, question, answer, skipped}
  started_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

CREATE TABLE roadmaps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  start_date date NOT NULL, end_date date NOT NULL,
  content jsonb NOT NULL,                      -- {milestone, weeks:[{week, focus, outcomes[]}]}
  prompt_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE weekly_kpis (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  roadmap_id     uuid REFERENCES roadmaps(id) ON DELETE CASCADE,
  week_number    int  NOT NULL CHECK (week_number >= 1),
  type           text NOT NULL CHECK (type IN ('professional', 'personal')),
  title          text NOT NULL,
  detail         text,
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'missed')),
  due_date       date NOT NULL,
  stretch_level  int  NOT NULL DEFAULT 2 CHECK (stretch_level BETWEEN 1 AND 3),
  replaced_by_guardrail boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz
);
CREATE INDEX weekly_kpis_user_week ON weekly_kpis (user_id, week_number);

CREATE TABLE sessions (                      -- a conversation session; new after 30 idle minutes
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  started_at        timestamptz NOT NULL DEFAULT now(),
  last_activity_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_last ON sessions (user_id, last_activity_at DESC);

CREATE TABLE reflections (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kpi_id       uuid NOT NULL REFERENCES weekly_kpis(id) ON DELETE CASCADE,
  week_number  int  NOT NULL,
  text_enc     text,                           -- NULL when skipped
  skipped      boolean NOT NULL DEFAULT false, -- "I'd rather not say" is a complete answer
  tags         text[] NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kpi_id)
);

CREATE TABLE messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id      uuid REFERENCES sessions(id) ON DELETE SET NULL,
  role            text NOT NULL CHECK (role IN ('founder', 'eir', 'system')),
  content_enc     text NOT NULL,
  channel         text NOT NULL DEFAULT 'chat' CHECK (channel IN ('chat', 'intake', 'reflection')),
  prompt_version  text,
  week_number     int,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_user_time ON messages (user_id, created_at);

CREATE TABLE feedback (                      -- founder ratings of EIR messages; drives adaptation
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_id   uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  rating       smallint NOT NULL CHECK (rating IN (-1, 1)),
  reasons      text[] NOT NULL DEFAULT '{}',
  comment_enc  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (message_id)
);

CREATE TABLE founder_state (                 -- flow state + what the EIR has learned about this founder
  user_id               uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  current_week          int  NOT NULL DEFAULT 0,
  paused                boolean NOT NULL DEFAULT false,
  pause_reason          text CHECK (pause_reason IN ('wellbeing', 'crisis', 'medical')),
  paused_at             timestamptz,
  wellbeing_weeks_left  int  NOT NULL DEFAULT 0,
  professional_count    int  NOT NULL DEFAULT 3 CHECK (professional_count BETWEEN 1 AND 3),
  professional_stretch  int  NOT NULL DEFAULT 2 CHECK (professional_stretch BETWEEN 1 AND 3),
  personal_stretch      int  NOT NULL DEFAULT 1 CHECK (personal_stretch BETWEEN 1 AND 2),
  tone                  text NOT NULL DEFAULT 'balanced' CHECK (tone IN ('gentle', 'balanced', 'direct')),
  learned               jsonb NOT NULL DEFAULT '[]'::jsonb,
  history               jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ safety (internal only)
CREATE TABLE zone_events (                   -- append-only: one row per classified founder message
  event_id            uuid PRIMARY KEY,
  user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id          uuid REFERENCES sessions(id) ON DELETE SET NULL,
  message_id          uuid REFERENCES messages(id) ON DELETE SET NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  zone                text NOT NULL CHECK (zone IN ('green', 'yellow', 'red')),
  model_zone          text CHECK (model_zone IN ('green', 'yellow', 'red')),   -- before signals and thresholds
  confidence          real CHECK (confidence BETWEEN 0 AND 1),
  categories          text[] NOT NULL DEFAULT '{}',
  rationale_enc       text,
  signals             jsonb NOT NULL DEFAULT '{}'::jsonb,
  escalation_reasons  text[] NOT NULL DEFAULT '{}',   -- why code raised the zone above the model's
  response_mode       text NOT NULL CHECK (response_mode IN ('normal', 'careful', 'red_check_in', 'red_crisis')),
  kpi_completion_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  classifier_version  text NOT NULL,
  prompt_version      text,
  sampled_for_review  boolean NOT NULL DEFAULT false   -- random GREEN sample, to measure misses
);
CREATE INDEX zone_events_user_time ON zone_events (user_id, created_at DESC);
CREATE INDEX zone_events_time ON zone_events (created_at DESC);

CREATE TABLE alerts (                        -- one per RED event
  id                 uuid PRIMARY KEY,
  event_id           uuid NOT NULL REFERENCES zone_events(event_id) ON DELETE CASCADE,
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at         timestamptz NOT NULL DEFAULT now(),
  status             text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
  acknowledged_by    uuid,
  acknowledged_at    timestamptz,
  resolved_at        timestamptz,
  first_notified_at  timestamptz,
  escalation_level   int NOT NULL DEFAULT 0,
  escalated_at       timestamptz,
  notifications      jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX alerts_open ON alerts (status, created_at);

CREATE TABLE staff (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email       text NOT NULL UNIQUE,
  name        text NOT NULL,
  role        text NOT NULL CHECK (role IN ('eir', 'safety_reviewer', 'admin')),
  safety_trained_at timestamptz NOT NULL,    -- only trained staff get accounts
  active      boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE staff_tokens (                  -- no RLS; reachable only via definer functions
  token_hash  text PRIMARY KEY,
  staff_id    uuid NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reviews (                       -- human review outcomes; feed classifier accuracy
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id      uuid NOT NULL REFERENCES zone_events(event_id) ON DELETE CASCADE,
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reviewer_id   uuid NOT NULL REFERENCES staff(id),
  verdict       text NOT NULL CHECK (verdict IN ('agree', 'disagree', 'escalated', 'resolved')),
  human_zone    text NOT NULL CHECK (human_zone IN ('green', 'yellow', 'red')),
  note_enc      text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reviews_event ON reviews (event_id);

CREATE TABLE audit_log (                     -- append-only record of every staff view of sensitive data
  id         bigserial PRIMARY KEY,
  staff_id   uuid NOT NULL,
  action     text NOT NULL,
  user_id    uuid,                           -- founder whose data was viewed (no FK: survives deletion)
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  at         timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ append-only guards
CREATE OR REPLACE FUNCTION block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('app.purge', true) = 'on' THEN   -- deletion or retention purge via definer functions
    IF TG_OP = 'UPDATE' THEN RETURN NEW; END IF;
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$;
CREATE TRIGGER zone_events_append_only BEFORE UPDATE OR DELETE ON zone_events
  FOR EACH ROW EXECUTE FUNCTION block_mutation();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION block_mutation();

-- Live stream: a NOTIFY with ids only (never text) for every zone event and alert change.
CREATE OR REPLACE FUNCTION notify_zone_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('aieir_live', json_build_object('type', 'zone_event', 'event_id', NEW.event_id,
                                                    'zone', NEW.zone)::text);
  RETURN NEW;
END $$;
CREATE TRIGGER zone_events_notify AFTER INSERT ON zone_events FOR EACH ROW EXECUTE FUNCTION notify_zone_event();
CREATE OR REPLACE FUNCTION notify_alert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('aieir_live', json_build_object('type', 'alert', 'alert_id', NEW.id,
                                                    'status', NEW.status)::text);
  RETURN NEW;
END $$;
CREATE TRIGGER alerts_notify AFTER INSERT OR UPDATE ON alerts FOR EACH ROW EXECUTE FUNCTION notify_alert();

-- ------------------------------------------------------------------ row-level security
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['startups', 'intake_interviews', 'roadmaps', 'weekly_kpis', 'sessions',
                           'reflections', 'messages', 'feedback', 'founder_state']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY founder_own ON %I TO aieir_app USING (user_id = app_current_user()) '
                   'WITH CHECK (user_id = app_current_user())', t);
    EXECUTE format('CREATE POLICY staff_read ON %I FOR SELECT TO aieir_staff USING (app_staff_role() IS NOT NULL)', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['users', 'zone_events', 'alerts', 'staff', 'reviews', 'audit_log']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

CREATE POLICY founder_own ON users TO aieir_app USING (id = app_current_user()) WITH CHECK (id = app_current_user());
CREATE POLICY staff_read ON users FOR SELECT TO aieir_staff USING (app_staff_role() IS NOT NULL);

-- Founder side may write safety records for itself, and may not read them.
CREATE POLICY founder_insert ON zone_events FOR INSERT TO aieir_app WITH CHECK (user_id = app_current_user());
CREATE POLICY staff_read ON zone_events FOR SELECT TO aieir_staff USING (app_staff_role() IS NOT NULL);
CREATE POLICY founder_insert ON alerts FOR INSERT TO aieir_app WITH CHECK (user_id = app_current_user());
CREATE POLICY staff_read ON alerts FOR SELECT TO aieir_staff USING (app_staff_role() IS NOT NULL);
CREATE POLICY staff_update ON alerts FOR UPDATE TO aieir_staff USING (app_staff_role() IS NOT NULL)
  WITH CHECK (app_staff_role() IS NOT NULL);

CREATE POLICY self_or_admin ON staff FOR SELECT TO aieir_staff USING (id = app_staff_id() OR app_staff_role() = 'admin');
CREATE POLICY admin_write ON staff FOR INSERT TO aieir_staff WITH CHECK (app_staff_role() = 'admin');
CREATE POLICY admin_update ON staff FOR UPDATE TO aieir_staff USING (app_staff_role() = 'admin') WITH CHECK (app_staff_role() = 'admin');

CREATE POLICY staff_read ON reviews FOR SELECT TO aieir_staff USING (app_staff_role() IS NOT NULL);
CREATE POLICY reviewer_insert ON reviews FOR INSERT TO aieir_staff
  WITH CHECK (reviewer_id = app_staff_id() AND app_staff_role() IN ('safety_reviewer', 'admin'));

CREATE POLICY staff_insert ON audit_log FOR INSERT TO aieir_staff WITH CHECK (staff_id = app_staff_id());
CREATE POLICY admin_read ON audit_log FOR SELECT TO aieir_staff USING (app_staff_role() = 'admin');

-- Cross-references must stay inside one founder even if an id is guessed.
CREATE POLICY own_message_only ON feedback AS RESTRICTIVE TO aieir_app
  USING (EXISTS (SELECT 1 FROM messages m WHERE m.id = message_id AND m.user_id = app_current_user()))
  WITH CHECK (EXISTS (SELECT 1 FROM messages m WHERE m.id = message_id AND m.user_id = app_current_user()));
CREATE POLICY own_kpi_only ON reflections AS RESTRICTIVE TO aieir_app
  USING (EXISTS (SELECT 1 FROM weekly_kpis k WHERE k.id = kpi_id AND k.user_id = app_current_user()))
  WITH CHECK (EXISTS (SELECT 1 FROM weekly_kpis k WHERE k.id = kpi_id AND k.user_id = app_current_user()));

-- ------------------------------------------------------------------ definer functions
CREATE OR REPLACE FUNCTION auth_user_by_token(p_token_hash text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT user_id FROM auth_tokens WHERE token_hash = p_token_hash
$$;

CREATE OR REPLACE FUNCTION create_founder(p_email text, p_name text, p_country text, p_token_hash text,
                                          p_consent_version text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE new_id uuid := gen_random_uuid();
BEGIN
  IF p_consent_version IS NULL OR p_consent_version = '' THEN
    RAISE EXCEPTION 'safety-review consent is required';
  END IF;
  PERFORM set_config('app.user_id', new_id::text, true);
  INSERT INTO users (id, email, display_name, country, consent_version, consented_at)
    VALUES (new_id, lower(p_email), p_name, coalesce(nullif(p_country, ''), 'US'), p_consent_version, now());
  INSERT INTO auth_tokens (token_hash, user_id) VALUES (p_token_hash, new_id);
  INSERT INTO founder_state (user_id) VALUES (new_id);
  RETURN new_id;
END $$;

CREATE OR REPLACE FUNCTION auth_staff_by_token(p_token_hash text) RETURNS TABLE (staff_id uuid, role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT s.id, s.role FROM staff_tokens t JOIN staff s ON s.id = t.staff_id
  WHERE t.token_hash = p_token_hash AND s.active
$$;

-- A founder deleting their account removes everything, including append-only safety rows.
-- Returns the ids of any unresolved RED alerts it removed, so on-call can be told (without
-- any founder details) that an open alert closed because the founder deleted their account.
CREATE OR REPLACE FUNCTION delete_my_data() RETURNS uuid[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE me uuid := app_current_user();
        open_alerts uuid[];
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'no founder in scope'; END IF;
  SELECT coalesce(array_agg(id), '{}') INTO open_alerts FROM alerts WHERE user_id = me AND status <> 'resolved';
  PERFORM set_config('app.purge', 'on', true);
  DELETE FROM users WHERE id = me;
  RETURN open_alerts;
END $$;

-- Retention: run daily by the maintenance job with the admin connection.
CREATE OR REPLACE FUNCTION purge_expired(p_days int) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE cutoff timestamptz := now() - make_interval(days => p_days);
        n_msg int; n_refl int; n_events int;
BEGIN
  PERFORM set_config('app.purge', 'on', true);
  DELETE FROM messages WHERE created_at < cutoff;            GET DIAGNOSTICS n_msg = ROW_COUNT;
  DELETE FROM reflections WHERE created_at < cutoff;         GET DIAGNOSTICS n_refl = ROW_COUNT;
  -- Safety events go too, except any still tied to an unresolved alert.
  DELETE FROM zone_events e WHERE e.created_at < cutoff
    AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.event_id = e.event_id AND a.status <> 'resolved');
  GET DIAGNOSTICS n_events = ROW_COUNT;
  DELETE FROM audit_log WHERE at < now() - make_interval(days => greatest(p_days, 730));
  RETURN jsonb_build_object('messages', n_msg, 'reflections', n_refl, 'zone_events', n_events);
END $$;

-- ------------------------------------------------------------------ grants
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aieir_app, aieir_staff;
GRANT EXECUTE ON FUNCTION app_current_user(), app_staff_id(), app_staff_role(), block_mutation(),
  notify_zone_event(), notify_alert() TO aieir_app, aieir_staff;

GRANT SELECT, INSERT, UPDATE, DELETE ON users, startups, intake_interviews, roadmaps, weekly_kpis, sessions,
  reflections, messages, feedback, founder_state TO aieir_app;
GRANT INSERT ON zone_events, alerts TO aieir_app;                       -- write-only
GRANT EXECUTE ON FUNCTION auth_user_by_token(text), create_founder(text, text, text, text, text),
  delete_my_data() TO aieir_app;

GRANT SELECT ON users, startups, intake_interviews, roadmaps, weekly_kpis, sessions, reflections, messages,
  feedback, founder_state, zone_events, reviews, audit_log, staff TO aieir_staff;
GRANT UPDATE (status, acknowledged_by, acknowledged_at, resolved_at, first_notified_at, escalation_level,
  escalated_at, notifications) ON alerts TO aieir_staff;
GRANT SELECT ON alerts TO aieir_staff;
GRANT INSERT ON reviews, audit_log TO aieir_staff;
GRANT INSERT, UPDATE ON staff TO aieir_staff;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO aieir_staff;
GRANT EXECUTE ON FUNCTION auth_staff_by_token(text) TO aieir_staff;
-- auth_tokens, staff_tokens, purge_expired: no grants to either app role.
