-- 007_annotation_speed_indexes.sql
-- Partial indexes for the public 28-day annotation-speed aggregate.
-- Added after EXPLAIN (ANALYZE, BUFFERS) on 100k published/annotated rows
-- showed a nested-loop join from version submitted_at to
-- annotation_tasks.current_published_version_id running for minutes.
-- (Transaction control is provided by the migration runner.)

CREATE INDEX IF NOT EXISTS idx_versions_published_annotated_submitted
    ON annotation_versions (submitted_at DESC, id DESC)
    WHERE lifecycle = 'published'
      AND target_status = 'annotated'
      AND submitted_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_current_published_annotated
    ON annotation_tasks (current_published_version_id)
    WHERE status = 'annotated'
      AND current_published_version_id IS NOT NULL;
