-- 008_cross_annotation_quality.sql — cross-check schema, constraints, backfill
-- Applied by: uv run python manage_state.py apply-migrations
-- Transaction control is provided by the migration runner.
-- Additive: existing task/version/assignment IDs, published pointers, and
-- human text are unchanged. Word-difference threshold is not a settings
-- column; each round stores 1000 bps with comparison_version worddiff_v1.

-- Composite unique key so round version FKs cannot point at another task.
ALTER TABLE annotation_versions
    ADD CONSTRAINT annotation_versions_task_id_id_key UNIQUE (task_id, id);

ALTER TABLE annotation_versions
    DROP CONSTRAINT annotation_versions_lifecycle_check;

ALTER TABLE annotation_versions
    ADD CONSTRAINT annotation_versions_lifecycle_check
        CHECK (lifecycle IN (
            'baseline', 'draft', 'published', 'superseded',
            'abandoned', 'revoked', 'cross_check_submitted'
        )),
    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'annotation',
    ADD COLUMN credited_annotator_id UUID REFERENCES annotators(id),
    ADD COLUMN published_by_admin_action_id UUID REFERENCES admin_actions(id),
    ADD CONSTRAINT annotation_versions_purpose_check
        CHECK (purpose IN ('annotation', 'cross_check', 'adjudication'));

-- Attribution only: existing submitted versions keep their real submitter.
-- Do not treat this column as participation proof in the backfill below.
UPDATE annotation_versions
SET credited_annotator_id = submitted_by_user_id
WHERE submitted_by_user_id IS NOT NULL
  AND credited_annotator_id IS NULL;

CREATE TABLE cross_check_settings (
    id                          SMALLINT PRIMARY KEY
                                CHECK (id = 1),
    enabled                     BOOLEAN NOT NULL DEFAULT false,
    sampling_rate_bps           INTEGER NOT NULL DEFAULT 1000
                                CHECK (sampling_rate_bps >= 0
                                       AND sampling_rate_bps <= 10000),
    revision                    INTEGER NOT NULL DEFAULT 0,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by_admin_action_id  UUID REFERENCES admin_actions(id)
);

INSERT INTO cross_check_settings (
    id, enabled, sampling_rate_bps, revision, updated_at
) VALUES (1, false, 1000, 0, now());

CREATE TABLE cross_check_rounds (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id                     UUID NOT NULL REFERENCES annotation_tasks(id),
    revision                    INTEGER NOT NULL DEFAULT 0,
    original_version_id         UUID NOT NULL,
    original_annotator_id       UUID REFERENCES annotators(id),
    original_review_id          UUID REFERENCES scene_reviews(id) ON DELETE SET NULL,
    secondary_version_id        UUID NOT NULL UNIQUE,
    secondary_annotator_id      UUID NOT NULL REFERENCES annotators(id),
    baseline_version_id         UUID NOT NULL,
    baseline_quality            TEXT NOT NULL
                                CHECK (baseline_quality IN (
                                    'exact', 'reconstructed', 'reprocessed'
                                )),
    state                       TEXT NOT NULL
                                CHECK (state IN (
                                    'in_progress', 'awaiting_review', 'passed',
                                    'adjudicated', 'cancelled', 'invalidated'
                                )),
    settings_revision           INTEGER NOT NULL,
    sampling_rate_bps           INTEGER NOT NULL
                                CHECK (sampling_rate_bps >= 0
                                       AND sampling_rate_bps <= 10000),
    claim_policy                TEXT NOT NULL,
    claim_filters               JSONB NOT NULL DEFAULT '{}'::jsonb,
    comparison_version          TEXT NOT NULL DEFAULT 'worddiff_v1',
    threshold_bps               INTEGER NOT NULL DEFAULT 1000
                                CHECK (threshold_bps >= 0
                                       AND threshold_bps <= 10000),
    original_word_count         INTEGER,
    secondary_word_count        INTEGER,
    edit_distance               INTEGER,
    substitutions               INTEGER,
    insertions                  INTEGER,
    deletions                   INTEGER,
    original_normalized_summary TEXT,
    secondary_normalized_summary TEXT,
    original_input_revision     INTEGER,
    secondary_input_revision    INTEGER,
    diff_ops                    JSONB,
    segment_map                 JSONB,
    reason_codes                TEXT[] NOT NULL DEFAULT '{}'::text[],
    decision                    TEXT
                                CHECK (decision IS NULL
                                       OR decision IN (
                                           'original', 'secondary', 'edited'
                                       )),
    final_version_id            UUID,
    decided_by_admin_action_id  UUID REFERENCES admin_actions(id),
    decision_reason             TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at                TIMESTAMPTZ,
    compared_at                 TIMESTAMPTZ,
    resolved_at                 TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    termination_reason          TEXT,
    CONSTRAINT cross_check_rounds_distinct_annotators
        CHECK (original_annotator_id IS DISTINCT FROM secondary_annotator_id),
    CONSTRAINT cross_check_rounds_distinct_versions
        CHECK (original_version_id <> secondary_version_id),
    CONSTRAINT cross_check_rounds_original_version_same_task
        FOREIGN KEY (task_id, original_version_id)
        REFERENCES annotation_versions (task_id, id),
    CONSTRAINT cross_check_rounds_secondary_version_same_task
        FOREIGN KEY (task_id, secondary_version_id)
        REFERENCES annotation_versions (task_id, id),
    CONSTRAINT cross_check_rounds_baseline_version_same_task
        FOREIGN KEY (task_id, baseline_version_id)
        REFERENCES annotation_versions (task_id, id),
    CONSTRAINT cross_check_rounds_final_version_same_task
        FOREIGN KEY (task_id, final_version_id)
        REFERENCES annotation_versions (task_id, id),
    -- NULL distance is not a zero-distance pass.
    CONSTRAINT cross_check_rounds_passed_state_check
        CHECK (
            state <> 'passed'
            OR (
                submitted_at IS NOT NULL
                AND compared_at IS NOT NULL
                AND edit_distance IS NOT NULL
                AND original_word_count IS NOT NULL
                AND secondary_word_count IS NOT NULL
                AND original_word_count > 0
                AND secondary_word_count > 0
                AND NOT (
                    reason_codes && ARRAY[
                        'word_difference_exceeded',
                        'submission_status_conflict',
                        'empty_original_text',
                        'empty_secondary_text',
                        'bad_quality_conflict',
                        'comparison_unavailable'
                    ]::text[]
                )
            )
        ),
    CONSTRAINT cross_check_rounds_awaiting_review_state_check
        CHECK (
            state <> 'awaiting_review'
            OR (
                submitted_at IS NOT NULL
                AND cardinality(reason_codes) >= 1
            )
        ),
    CONSTRAINT cross_check_rounds_adjudicated_state_check
        CHECK (
            state <> 'adjudicated'
            OR (
                final_version_id IS NOT NULL
                AND decision IS NOT NULL
                AND decided_by_admin_action_id IS NOT NULL
            )
        )
);

CREATE UNIQUE INDEX uniq_open_cross_check_round_per_task
    ON cross_check_rounds (task_id)
    WHERE state IN ('in_progress', 'awaiting_review');

CREATE UNIQUE INDEX uniq_active_cross_check_original_version
    ON cross_check_rounds (original_version_id)
    WHERE state NOT IN ('cancelled', 'invalidated');

CREATE INDEX idx_cross_check_rounds_final_version
    ON cross_check_rounds (final_version_id)
    WHERE final_version_id IS NOT NULL;

CREATE INDEX idx_cross_check_rounds_state_created
    ON cross_check_rounds (state, created_at, id);

CREATE INDEX idx_cross_check_rounds_secondary_submitted
    ON cross_check_rounds (secondary_annotator_id, submitted_at, id);

CREATE INDEX idx_cross_check_rounds_task_created
    ON cross_check_rounds (task_id, created_at, id);

-- MATCH SIMPLE: ordinary annotation/revision rows have round_id NULL, so
-- the composite FK is not checked. in_progress rounds store the draft as
-- secondary_version_id before the assignment is inserted.
ALTER TABLE cross_check_rounds
    ADD CONSTRAINT cross_check_rounds_assignment_match_key
        UNIQUE (id, task_id, secondary_annotator_id, secondary_version_id);

ALTER TABLE assignments
    DROP CONSTRAINT assignments_mode_check;

ALTER TABLE assignments
    ADD CONSTRAINT assignments_mode_check
        CHECK (mode IN ('annotation', 'revision', 'cross_check')),
    ADD COLUMN cross_check_round_id UUID,
    ADD CONSTRAINT assignments_cross_check_round_id_check
        CHECK (
            (mode = 'cross_check' AND cross_check_round_id IS NOT NULL)
            OR (mode <> 'cross_check' AND cross_check_round_id IS NULL)
        ),
    ADD CONSTRAINT assignments_cross_check_round_match
        FOREIGN KEY (cross_check_round_id, task_id, user_id, working_version_id)
        REFERENCES cross_check_rounds (
            id, task_id, secondary_annotator_id, secondary_version_id
        );

CREATE TABLE task_annotation_participants (
    task_id                 UUID NOT NULL REFERENCES annotation_tasks(id) ON DELETE CASCADE,
    user_id                 UUID NOT NULL REFERENCES annotators(id) ON DELETE CASCADE,
    first_participated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, user_id)
);

CREATE INDEX idx_task_annotation_participants_user
    ON task_annotation_participants (user_id, task_id);

-- Union of real human participation. Skip NULL user/task ids and system
-- events such as imported. credited_annotator_id is not a source.
INSERT INTO task_annotation_participants (task_id, user_id, first_participated_at)
SELECT src.task_id, src.user_id, min(src.first_at)
FROM (
    SELECT a.task_id, a.user_id, a.assigned_at AS first_at
    FROM assignments a
    WHERE a.task_id IS NOT NULL AND a.user_id IS NOT NULL

    UNION ALL

    SELECT v.task_id, v.created_by_user_id, v.created_at
    FROM annotation_versions v
    WHERE v.created_by_user_id IS NOT NULL

    UNION ALL

    SELECT v.task_id, v.modified_by_user_id, v.updated_at
    FROM annotation_versions v
    WHERE v.modified_by_user_id IS NOT NULL

    UNION ALL

    SELECT v.task_id, v.submitted_by_user_id,
           COALESCE(v.submitted_at, v.updated_at, v.created_at)
    FROM annotation_versions v
    WHERE v.submitted_by_user_id IS NOT NULL

    UNION ALL

    SELECT e.task_id, e.user_id, e.created_at
    FROM annotation_events e
    WHERE e.task_id IS NOT NULL
      AND e.user_id IS NOT NULL
      AND e.event_type IN ('claimed', 'reopened', 'completed', 'abandoned')
) src
GROUP BY src.task_id, src.user_id;
