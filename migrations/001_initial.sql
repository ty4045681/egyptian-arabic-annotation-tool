-- 001_initial.sql — PostgreSQL schema for the annotation platform
-- Applied by: uv run python manage_state.py apply-migrations
-- NEVER auto-applied by Gunicorn workers.
-- (Transaction control is provided by the migration runner.)

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stable claim ordering. Migration assigns explicit values for legacy tasks
-- and then advances the sequence; preprocess takes nextval() for new tasks.
CREATE SEQUENCE task_allocation_order_seq START 1;

-- ============================================================
-- Users and sessions
-- ============================================================
CREATE TABLE annotators (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One active web session per username. Replacing the row (new session_id)
-- invalidates the old cookie. expires_at drives same-name login conflicts.
CREATE TABLE active_sessions (
    user_id     UUID PRIMARY KEY REFERENCES annotators(id) ON DELETE CASCADE,
    session_id  UUID NOT NULL UNIQUE,
    login_time  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- ============================================================
-- Tasks and versioned annotations
-- ============================================================
CREATE TABLE annotation_tasks (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rel_path                    TEXT NOT NULL UNIQUE,          -- audio path relative to audio_dir, e.g. 'folder/a.wav'
    legacy_audio_key            TEXT,                          -- old JSON key ('folder/a'), for migration mapping only
    filename                    TEXT NOT NULL,
    folder                      TEXT NOT NULL DEFAULT '',
    duration                    DOUBLE PRECISION NOT NULL DEFAULT 0,
    status                      TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','annotated','skipped')),
    eligible                    BOOLEAN NOT NULL DEFAULT true, -- false = excluded from claiming (e.g. no segments)
    allocation_order            BIGINT NOT NULL UNIQUE,        -- stable claim ordering (no ORDER BY random())
    current_published_version_id UUID,
    reserved_for_user_id        UUID REFERENCES annotators(id),-- migration orphan drafts: return to original user first
    category                    TEXT,
    preprocessed_at             TIMESTAMPTZ,
    source_json_sha256          TEXT,                          -- provenance for migrated tasks
    extra                       JSONB NOT NULL DEFAULT '{}'::jsonb, -- unknown legacy top-level fields
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE annotation_versions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                 UUID NOT NULL REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    version_no              INTEGER NOT NULL,
    lifecycle               TEXT NOT NULL
                            CHECK (lifecycle IN ('draft','published','superseded','abandoned')),
    target_status           TEXT NOT NULL DEFAULT 'pending'
                            CHECK (target_status IN ('pending','annotated','skipped')),
    base_version_id         UUID REFERENCES annotation_versions(id),
    revision                INTEGER NOT NULL DEFAULT 0,       -- optimistic concurrency, bumped on every draft save
    human_modified          BOOLEAN NOT NULL DEFAULT false,
    created_by_user_id      UUID REFERENCES annotators(id),
    modified_by_user_id     UUID REFERENCES annotators(id),
    submitted_by_user_id    UUID REFERENCES annotators(id),
    skip_reasons            TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at            TIMESTAMPTZ,
    extra                   JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (task_id, version_no)
);

-- At most one live draft and one live published version per task.
CREATE UNIQUE INDEX uniq_draft_per_task      ON annotation_versions(task_id) WHERE lifecycle = 'draft';
CREATE UNIQUE INDEX uniq_published_per_task  ON annotation_versions(task_id) WHERE lifecycle = 'published';

-- task -> current published version (added after versions table exists)
ALTER TABLE annotation_tasks
    ADD CONSTRAINT fk_task_published_version
    FOREIGN KEY (current_published_version_id)
    REFERENCES annotation_versions(id);

CREATE TABLE segments (
    version_id              UUID NOT NULL REFERENCES annotation_versions(id) ON DELETE CASCADE,
    segment_id              INTEGER NOT NULL,
    start_s                 DOUBLE PRECISION NOT NULL,
    end_s                   DOUBLE PRECISION NOT NULL,
    duration                DOUBLE PRECISION NOT NULL,
    asr_text                TEXT NOT NULL DEFAULT '',
    text                    TEXT NOT NULL DEFAULT '',
    exclude_from_training   BOOLEAN NOT NULL DEFAULT false,
    extra                   JSONB NOT NULL DEFAULT '{}'::jsonb, -- unknown legacy segment fields
    PRIMARY KEY (version_id, segment_id)
);

CREATE TABLE waveforms (
    task_id         UUID PRIMARY KEY REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    encoding        TEXT NOT NULL DEFAULT 'int16le',
    point_count     INTEGER NOT NULL,
    payload         BYTEA NOT NULL,            -- raw int16 LE samples (legacy base64 decoded)
    checksum        TEXT NOT NULL              -- sha256 of payload
);

-- ============================================================
-- Assignments (task leases)
-- ============================================================
CREATE TABLE assignments (
    user_id             UUID PRIMARY KEY REFERENCES annotators(id) ON DELETE CASCADE,
    task_id             UUID NOT NULL UNIQUE REFERENCES annotation_tasks(id),
    working_version_id  UUID NOT NULL UNIQUE REFERENCES annotation_versions(id),
    mode                TEXT NOT NULL CHECK (mode IN ('annotation','revision')),
    lease_token         UUID NOT NULL UNIQUE,
    base_revision       INTEGER NOT NULL,
    assigned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    legacy_meta         JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ============================================================
-- History and globally unique idempotency operations
-- ============================================================
CREATE TABLE operations (
    operation_id    UUID PRIMARY KEY,
    user_id         UUID REFERENCES annotators(id),
    route           TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE annotation_events (
    id              BIGSERIAL PRIMARY KEY,
    operation_id    UUID UNIQUE REFERENCES operations(operation_id),
    user_id         UUID REFERENCES annotators(id),
    task_id         UUID REFERENCES annotation_tasks(id),
    version_id      UUID REFERENCES annotation_versions(id),
    event_type      TEXT NOT NULL,   -- claimed/abandoned/completed/reopened/imported/released_admin
    from_status     TEXT,
    to_status       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    details         JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- JSON migration audit. Batches are resumable by manifest hash; every
-- imported source file keeps its exact SHA and task mapping.
CREATE TABLE import_batches (
    id              UUID PRIMARY KEY,
    source_root     TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    counts          JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT
);

CREATE TABLE import_items (
    batch_id        UUID NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    source_path     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    semantic_sha256 TEXT NOT NULL,
    task_id         UUID REFERENCES annotation_tasks(id),
    result          TEXT NOT NULL CHECK (result IN ('imported','skipped','failed')),
    error           TEXT,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, source_path)
);

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX idx_tasks_claim          ON annotation_tasks(status, eligible, allocation_order);
CREATE INDEX idx_tasks_reserved       ON annotation_tasks(reserved_for_user_id)
                                       WHERE reserved_for_user_id IS NOT NULL;
CREATE INDEX idx_tasks_status_updated ON annotation_tasks(status, updated_at DESC);
CREATE INDEX idx_versions_task        ON annotation_versions(task_id, version_no);
CREATE INDEX idx_versions_submitter   ON annotation_versions(submitted_by_user_id, submitted_at DESC)
                                       WHERE submitted_by_user_id IS NOT NULL;
CREATE INDEX idx_events_user_time     ON annotation_events(user_id, created_at DESC);
CREATE INDEX idx_events_task_time     ON annotation_events(task_id, created_at DESC);
CREATE INDEX idx_segments_version     ON segments(version_id);
