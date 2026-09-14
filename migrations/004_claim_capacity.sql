-- 004_claim_capacity.sql — indexes for 100k-task claim / list / overview paths
-- Applied by: uv run python manage_state.py apply-migrations
-- Transaction control is provided by the migration runner.
-- Additive only: no column drops, no constraint weakening.

-- Pending claim walk: allocation_order then id (SKIP LOCKED LIMIT 1).
CREATE INDEX IF NOT EXISTS idx_tasks_claim_pending_order
    ON annotation_tasks (allocation_order, id)
    WHERE status = 'pending' AND eligible;

-- Source-first candidate sets (high-rare / named scene / empty scene).
CREATE INDEX IF NOT EXISTS idx_task_sources_conf_scene_task
    ON task_sources (confidence, scene_code, task_id)
    WHERE is_current;

-- Admin 50-row newest-first list (created_at, id) keyset.
CREATE INDEX IF NOT EXISTS idx_tasks_created_id
    ON annotation_tasks (created_at DESC, id DESC);

-- Overview source groups: index-only by task of current evidence.
CREATE INDEX IF NOT EXISTS idx_task_sources_task_overview
    ON task_sources (task_id, scene_code, batch_id, confidence, id)
    WHERE is_current;
