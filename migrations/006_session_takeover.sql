-- 006_session_takeover.sql
-- Additive session takeover / idle+absolute timeout columns.
-- Keeps expires_at so same-schema rollback of old code still works.
-- (Transaction control is provided by the migration runner.)

ALTER TABLE active_sessions
    ADD COLUMN IF NOT EXISTS generation BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS absolute_expires_at TIMESTAMPTZ;

-- Online users at deploy get a smooth transition rather than a mass logout
-- from a historically early login_time.
UPDATE active_sessions
SET last_activity_at = LEAST(last_seen_at, expires_at),
    absolute_expires_at = now() + interval '20 hours'
WHERE last_activity_at IS NULL OR absolute_expires_at IS NULL;

-- Flatten historical anomalies before the new code semantics apply.
UPDATE active_sessions
SET expires_at = LEAST(expires_at, absolute_expires_at);

ALTER TABLE active_sessions
    ALTER COLUMN last_activity_at SET NOT NULL,
    ALTER COLUMN absolute_expires_at SET NOT NULL,
    ALTER COLUMN last_activity_at SET DEFAULT now(),
    ALTER COLUMN absolute_expires_at SET DEFAULT (now() + interval '20 hours');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'active_sessions_generation_positive'
    ) THEN
        ALTER TABLE active_sessions
            ADD CONSTRAINT active_sessions_generation_positive CHECK (generation > 0);
    END IF;
END $$;

COMMENT ON COLUMN active_sessions.last_seen_at IS
    'Presence heartbeat; does not by itself extend idle expiry';
COMMENT ON COLUMN active_sessions.expires_at IS
    'Server-enforced idle deadline';
COMMENT ON COLUMN active_sessions.absolute_expires_at IS
    'Non-sliding absolute session deadline';

CREATE TABLE IF NOT EXISTS annotator_session_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES annotators(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    generation BIGINT NOT NULL,
    previous_generation BIGINT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_annotator_session_events_user_time
    ON annotator_session_events(user_id, occurred_at DESC);
