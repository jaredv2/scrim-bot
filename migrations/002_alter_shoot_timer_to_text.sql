-- Migration: shoot_timer INTEGER → TEXT
-- Allows string timers like "02:30", "5m", "1m30s" instead of integer seconds
-- Run once on existing DBs; new DBs already have TEXT via supabase_schema.sql

-- Only alter if still INTEGER (idempotent)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'vtx_events'
      AND column_name = 'shoot_timer'
      AND data_type = 'integer'
  ) THEN
    ALTER TABLE vtx_events ALTER COLUMN shoot_timer TYPE TEXT USING shoot_timer::TEXT;
  END IF;
END $$;

-- Ensure default is text '0'
ALTER TABLE vtx_events ALTER COLUMN shoot_timer SET DEFAULT '0';

-- Verify
-- SELECT column_name, data_type, column_default FROM information_schema.columns WHERE table_name='vtx_events' AND column_name='shoot_timer';
