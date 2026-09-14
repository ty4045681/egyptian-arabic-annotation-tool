-- 003_scene_provenance.sql — source scenes, confidence, reviews, and claim context
-- Applied by: uv run python manage_state.py apply-migrations
-- Transaction control is provided by the migration runner.
-- Additive only: existing task/version/assignment IDs and human text are unchanged.

-- ============================================================
-- Scene dictionary (stable codes; aliases live in the adapter layer)
-- ============================================================
CREATE TABLE scenes (
    code        TEXT PRIMARY KEY,
    label_zh    TEXT NOT NULL,
    label_en    TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

INSERT INTO scenes (code, label_zh, label_en, sort_order) VALUES
    ('airport', '机场', 'Airport', 1),
    ('tourism_information', '旅游信息', 'Tourism information', 2),
    ('shopping', '购物', 'Shopping', 3),
    ('clinic', '诊所', 'Clinic', 4),
    ('emergencies', '紧急情况', 'Emergencies', 5),
    ('business_negotiation', '商务谈判', 'Business negotiation', 6),
    ('restaurant', '餐厅', 'Restaurant', 7),
    ('hotel', '酒店', 'Hotel', 8),
    ('taxi', '出租车', 'Taxi', 9);

-- ============================================================
-- Source batches and import runs (separate from legacy JSON import_batches)
-- ============================================================
CREATE TABLE source_batches (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_code  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    source_note TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE source_import_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id        UUID NOT NULL REFERENCES source_batches(id),
    snapshot_sha256 TEXT NOT NULL,
    snapshot_bytes  BIGINT,
    contract_version INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL
                    CHECK (status IN ('running', 'completed', 'failed', 'partial')),
    processed_count INTEGER NOT NULL DEFAULT 0,
    counts          JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_report    JSONB NOT NULL DEFAULT '[]'::jsonb,
    checkpoint      JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    UNIQUE (batch_id, snapshot_sha256)
);

-- ============================================================
-- External media identity (same video/variant shares one task)
-- ============================================================
CREATE TABLE task_media_identities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    variant         TEXT NOT NULL DEFAULT 'pcm16k_mono',
    pcm_sha256      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, external_id, variant)
);

CREATE INDEX idx_task_media_identities_task
    ON task_media_identities(task_id);

-- ============================================================
-- Versioned source evidence (never overwritten in place)
-- ============================================================
CREATE TABLE task_sources (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             UUID NOT NULL REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    batch_id            UUID NOT NULL REFERENCES source_batches(id),
    record_key          TEXT NOT NULL,
    revision            INTEGER NOT NULL DEFAULT 1,
    is_current          BOOLEAN NOT NULL DEFAULT true,
    scene_code          TEXT REFERENCES scenes(code),
    confidence          TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (confidence IN ('high', 'medium', 'low', 'unknown')),
    confidence_basis    TEXT NOT NULL DEFAULT '',
    source_type         TEXT NOT NULL DEFAULT '',
    source_url          TEXT,
    video_id            TEXT,
    channel_id          TEXT,
    channel_title       TEXT,
    provider            TEXT,
    raw_record          JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_digest      TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uniq_task_sources_current
    ON task_sources(batch_id, record_key) WHERE is_current;

CREATE INDEX idx_task_sources_task_current
    ON task_sources(task_id) WHERE is_current;

CREATE INDEX idx_task_sources_task_rank
    ON task_sources(task_id, confidence, scene_code, id) WHERE is_current;

CREATE INDEX idx_task_sources_scene_conf_batch
    ON task_sources(scene_code, confidence, batch_id, task_id) WHERE is_current;

CREATE INDEX idx_task_sources_batch_current
    ON task_sources(batch_id) WHERE is_current;

-- ============================================================
-- Model predictions (independent of source confidence and human review)
-- ============================================================
CREATE TABLE task_scene_predictions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                 UUID NOT NULL REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    input_version_id        UUID REFERENCES annotation_versions(id),
    input_revision          INTEGER,
    input_digest            TEXT NOT NULL,
    model_name              TEXT NOT NULL,
    prompt_version          TEXT NOT NULL DEFAULT '',
    predicted_label         TEXT NOT NULL,
    predicted_scene_code    TEXT REFERENCES scenes(code),
    score                   DOUBLE PRECISION,
    score_meaning           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (score IS NULL OR (score >= 0 AND score <= 1))
);

CREATE INDEX idx_predictions_task_time
    ON task_scene_predictions(task_id, created_at DESC);

-- ============================================================
-- Append-only human scene reviews, linked to annotation versions
-- ============================================================
CREATE TABLE scene_reviews (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id              UUID NOT NULL REFERENCES annotation_versions(id) ON DELETE CASCADE,
    review_no               INTEGER NOT NULL,
    status                  TEXT NOT NULL
                            CHECK (status IN (
                                'pending', 'confirmed', 'mixed',
                                'out_of_scope', 'uncertain'
                            )),
    note                    TEXT NOT NULL DEFAULT '',
    actor_user_id           UUID REFERENCES annotators(id),
    actor_admin_action_id   UUID REFERENCES admin_actions(id),
    actor_kind              TEXT NOT NULL
                            CHECK (actor_kind IN ('annotator', 'admin', 'system')),
    operation_id            UUID,
    previous_review_id      UUID REFERENCES scene_reviews(id),
    superseded              BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version_id, review_no)
);

CREATE INDEX idx_scene_reviews_version
    ON scene_reviews(version_id, review_no DESC);

CREATE TABLE scene_review_labels (
    review_id   UUID NOT NULL REFERENCES scene_reviews(id) ON DELETE CASCADE,
    scene_code  TEXT NOT NULL REFERENCES scenes(code),
    PRIMARY KEY (review_id, scene_code)
);

-- ============================================================
-- Annotator scene scope (missing row means all; empty restricted list means none)
-- ============================================================
CREATE TABLE annotator_scene_scopes (
    user_id         UUID PRIMARY KEY REFERENCES annotators(id) ON DELETE CASCADE,
    mode            TEXT NOT NULL CHECK (mode IN ('all', 'restricted', 'none')),
    allow_unknown   BOOLEAN NOT NULL DEFAULT false,
    revision        INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE annotator_scene_access (
    user_id     UUID NOT NULL REFERENCES annotators(id) ON DELETE CASCADE,
    scene_code  TEXT NOT NULL REFERENCES scenes(code),
    PRIMARY KEY (user_id, scene_code)
);

INSERT INTO annotator_scene_scopes (user_id, mode, allow_unknown, revision)
SELECT id, 'all', false, 0 FROM annotators
ON CONFLICT (user_id) DO NOTHING;

-- ============================================================
-- Claim context on assignments; processing lease on tasks
-- ============================================================
ALTER TABLE assignments
    ADD COLUMN claim_scene_code TEXT REFERENCES scenes(code),
    ADD COLUMN claim_source_id UUID REFERENCES task_sources(id),
    ADD COLUMN claim_policy TEXT NOT NULL DEFAULT 'fifo',
    ADD COLUMN claim_confidence TEXT
        CHECK (claim_confidence IS NULL
               OR claim_confidence IN ('high', 'medium', 'low', 'unknown'));

ALTER TABLE annotation_tasks
    ADD COLUMN processing_token UUID,
    ADD COLUMN processing_lease_until TIMESTAMPTZ,
    ADD COLUMN processing_version INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN pcm_sha256 TEXT;

CREATE INDEX idx_tasks_processing_lease
    ON annotation_tasks(processing_lease_until)
    WHERE processing_token IS NOT NULL;
