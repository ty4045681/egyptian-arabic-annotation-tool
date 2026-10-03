-- Preserve credited historical seconds; future submissions snapshot trainable segments.
ALTER TABLE annotation_versions
    ADD COLUMN annotation_duration_seconds DOUBLE PRECISION,
    ADD COLUMN annotation_duration_basis TEXT;

UPDATE annotation_versions v
SET annotation_duration_seconds = CASE
        WHEN v.target_status = 'annotated' THEN t.duration ELSE 0 END,
    annotation_duration_basis = CASE
        WHEN v.target_status = 'annotated' THEN 'legacy_audio_v1'
        ELSE 'trainable_segments_v1' END
FROM annotation_tasks t
WHERE t.id = v.task_id
  AND v.lifecycle IN ('published', 'superseded', 'revoked', 'cross_check_submitted');

ALTER TABLE annotation_versions
    ADD CONSTRAINT annotation_duration_pair CHECK (
        (annotation_duration_seconds IS NULL) = (annotation_duration_basis IS NULL)
    ),
    ADD CONSTRAINT annotation_duration_value CHECK (
        annotation_duration_seconds >= 0
        AND annotation_duration_seconds < 'Infinity'::double precision
    ),
    ADD CONSTRAINT annotation_duration_basis_check CHECK (
        annotation_duration_basis IN ('legacy_audio_v1', 'trainable_segments_v1')
    ),
    ADD CONSTRAINT annotation_duration_completed CHECK (
        lifecycle NOT IN ('published', 'superseded', 'revoked', 'cross_check_submitted')
        OR annotation_duration_seconds IS NOT NULL
    );
