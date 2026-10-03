"""Freeze annotation credit independently of the original media duration."""


def snapshot_annotation_duration(cur, version_id, target_status: str) -> None:
    """Called after segment validation, within the existing submission transaction."""
    cur.execute(
        """WITH credit AS (
               SELECT v.id,
                      CASE WHEN a.mode = 'revision'
                             AND v.purpose = 'annotation'
                             AND a.user_id = COALESCE(
                                 base.credited_annotator_id, base.submitted_by_user_id)
                             AND base.annotation_duration_basis = 'legacy_audio_v1'
                           THEN base.annotation_duration_seconds END AS legacy_seconds
               FROM annotation_versions v
               LEFT JOIN annotation_versions base
                 ON base.id = v.base_version_id AND base.task_id = v.task_id
               LEFT JOIN assignments a ON a.working_version_id = v.id
               WHERE v.id = %s AND v.lifecycle = 'draft'
                 AND v.annotation_duration_seconds IS NULL
           )
           UPDATE annotation_versions v
           SET annotation_duration_seconds = CASE
                 WHEN credit.legacy_seconds IS NOT NULL THEN credit.legacy_seconds
                 WHEN %s = 'annotated' THEN COALESCE((
                     SELECT sum(s.duration) FROM segments s
                     WHERE s.version_id = v.id AND NOT s.exclude_from_training
                 ), 0)
                 ELSE 0 END,
               annotation_duration_basis = CASE
                 WHEN credit.legacy_seconds IS NOT NULL THEN 'legacy_audio_v1'
                 ELSE 'trainable_segments_v1' END
           FROM credit WHERE v.id = credit.id""",
        (version_id, target_status),
    )
    if cur.rowcount != 1:
        raise RuntimeError("Annotation duration requires an unsubmitted draft")
