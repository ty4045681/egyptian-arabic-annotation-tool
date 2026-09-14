-- 002_admin.sql — Admin console, immutable baselines and reversible moderation
-- Applied by: uv run python manage_state.py apply-migrations
-- Transaction control is provided by the migration runner.

-- ============================================================
-- Annotator lifecycle
-- ============================================================
ALTER TABLE annotators
    ADD COLUMN status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN deactivated_at TIMESTAMPTZ,
    ADD COLUMN deactivated_reason TEXT,
    ADD CONSTRAINT annotators_status_check
        CHECK (status IN ('active', 'deactivated'));

-- ============================================================
-- Admin sessions and durable audit
-- ============================================================
CREATE TABLE admin_sessions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id              TEXT NOT NULL,
    token_digest        TEXT NOT NULL UNIQUE,
    csrf_digest         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    idle_expires_at     TIMESTAMPTZ NOT NULL,
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,
    ip_hash             TEXT,
    user_agent          TEXT,
    CONSTRAINT admin_session_token_digest_check
        CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT admin_session_csrf_digest_check
        CHECK (csrf_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT admin_session_expiry_order
        CHECK (idle_expires_at <= absolute_expires_at)
);

CREATE TABLE admin_actions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id        UUID NOT NULL UNIQUE,
    admin_session_id    UUID REFERENCES admin_sessions(id),
    admin_key_id        TEXT,
    action_type         TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    request_hash        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('completed', 'failed')),
    request             JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE TABLE admin_action_items (
    id                  BIGSERIAL PRIMARY KEY,
    admin_action_id     UUID NOT NULL REFERENCES admin_actions(id) ON DELETE CASCADE,
    task_id             UUID REFERENCES annotation_tasks(id),
    annotator_id        UUID REFERENCES annotators(id),
    expected_version_id UUID REFERENCES annotation_versions(id),
    before_version_id   UUID REFERENCES annotation_versions(id),
    after_version_id    UUID REFERENCES annotation_versions(id),
    result              TEXT NOT NULL,
    details             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Baseline and revoked annotation versions
-- ============================================================
ALTER TABLE annotation_versions
    DROP CONSTRAINT annotation_versions_lifecycle_check;

ALTER TABLE annotation_versions
    ADD CONSTRAINT annotation_versions_lifecycle_check
        CHECK (lifecycle IN (
            'baseline', 'draft', 'published', 'superseded',
            'abandoned', 'revoked'
        )),
    ADD COLUMN revoked_at TIMESTAMPTZ,
    ADD COLUMN revoked_reason TEXT,
    ADD COLUMN revoked_by_admin_action_id UUID REFERENCES admin_actions(id);

ALTER TABLE annotation_tasks
    ADD COLUMN baseline_version_id UUID REFERENCES annotation_versions(id),
    ADD COLUMN baseline_quality TEXT
        CHECK (baseline_quality IN ('exact', 'reconstructed', 'reprocessed'));

CREATE UNIQUE INDEX uniq_baseline_per_task
    ON annotation_versions(task_id) WHERE lifecycle = 'baseline';

-- Backfill one immutable baseline for every existing task. A pending,
-- untouched draft is an exact source. Historical/human versions retain the
-- best available ASR and boundaries, but human text/quality flags are removed.
WITH source_versions AS (
    SELECT t.id AS task_id,
           source.id AS source_version_id,
           source.extra AS source_extra,
           CASE
             WHEN t.status = 'pending'
              AND source.lifecycle = 'draft'
              AND NOT source.human_modified
             THEN 'exact'
             ELSE 'reconstructed'
           END AS quality,
           source.max_version_no + 1 AS baseline_version_no
    FROM annotation_tasks t
    JOIN LATERAL (
        SELECT v.id, v.lifecycle, v.human_modified, v.extra,
               max(v.version_no) OVER () AS max_version_no
        FROM annotation_versions v
        WHERE v.task_id = t.id
        ORDER BY
            CASE
              WHEN t.status = 'pending'
               AND v.lifecycle = 'draft'
               AND NOT v.human_modified
              THEN 0 ELSE 1
            END,
            v.version_no,
            v.id
        LIMIT 1
    ) source ON true
), inserted_baselines AS (
    INSERT INTO annotation_versions
           (id, task_id, version_no, lifecycle, target_status,
            base_version_id, revision, human_modified, created_at,
            updated_at, extra)
    SELECT gen_random_uuid(), source.task_id, source.baseline_version_no,
           'baseline', 'pending', source.source_version_id, 0, false,
           now(), now(),
           COALESCE(source.source_extra, '{}'::jsonb)
             || jsonb_build_object(
                    'baseline_source_version_id', source.source_version_id,
                    'baseline_quality', source.quality,
                    'backfilled_by_migration', 2
                )
    FROM source_versions source
    RETURNING id, task_id, base_version_id
)
INSERT INTO segments
       (version_id, segment_id, start_s, end_s, duration, asr_text,
        text, exclude_from_training, extra)
SELECT baseline.id, segment.segment_id, segment.start_s, segment.end_s,
       segment.duration, segment.asr_text,
       CASE WHEN source.quality = 'exact' THEN segment.text ELSE '' END,
       CASE WHEN source.quality = 'exact'
            THEN segment.exclude_from_training ELSE false END,
       segment.extra
FROM inserted_baselines baseline
JOIN source_versions source ON source.task_id = baseline.task_id
JOIN segments segment ON segment.version_id = baseline.base_version_id;

UPDATE annotation_tasks task
SET baseline_version_id = baseline.id,
    baseline_quality = baseline.extra->>'baseline_quality'
FROM annotation_versions baseline
WHERE baseline.task_id = task.id
  AND baseline.lifecycle = 'baseline';

UPDATE annotation_versions draft
SET base_version_id = task.baseline_version_id
FROM annotation_tasks task
WHERE draft.task_id = task.id
  AND draft.lifecycle = 'draft'
  AND task.baseline_version_id IS NOT NULL;

-- base_version_id on a baseline was used only to make the backfill copy
-- deterministic. The durable relationship points in the other direction.
UPDATE annotation_versions
SET base_version_id = NULL
WHERE lifecycle = 'baseline';

-- Prevent a revoked submitter from receiving the same task again.
CREATE TABLE task_annotator_blocks (
    task_id         UUID NOT NULL REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES annotators(id) ON DELETE CASCADE,
    reason          TEXT NOT NULL,
    admin_action_id UUID NOT NULL REFERENCES admin_actions(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, user_id)
);

ALTER TABLE annotation_events
    ADD COLUMN admin_action_id UUID REFERENCES admin_actions(id);

-- ============================================================
-- Query indexes
-- ============================================================
CREATE INDEX idx_annotators_status_created
    ON annotators(status, created_at, id);
CREATE INDEX idx_admin_sessions_token
    ON admin_sessions(token_digest)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_admin_actions_created
    ON admin_actions(created_at DESC, id DESC);
CREATE INDEX idx_admin_actions_type_created
    ON admin_actions(action_type, created_at DESC, id DESC);
CREATE INDEX idx_admin_action_items_task
    ON admin_action_items(task_id, admin_action_id);
CREATE INDEX idx_admin_action_items_annotator
    ON admin_action_items(annotator_id, admin_action_id);
CREATE INDEX idx_task_annotator_blocks_user
    ON task_annotator_blocks(user_id, task_id);
CREATE INDEX idx_versions_submitted_time
    ON annotation_versions(submitted_at DESC, id DESC)
    WHERE submitted_at IS NOT NULL;
CREATE INDEX idx_versions_revoked_time
    ON annotation_versions(revoked_at DESC, id DESC)
    WHERE lifecycle = 'revoked';
CREATE INDEX idx_events_type_created
    ON annotation_events(event_type, created_at DESC, id DESC);
