-- 005_ten_scene_catalog.sql — ten-scene English catalog and Spoken languages
-- Applied by: uv run python manage_state.py apply-migrations
-- Transaction control is provided by the migration runner.
-- Additive/idempotent: does not rewrite source.raw_record, reviews, predictions,
-- media, annotation contents, or historical claim rows. NULL source.scene_code
-- stays NULL; effective Spoken languages is derived in shared SQL/serializers.

-- Canonical codes, English labels, and display order. Chinese labels remain
-- for crawler/export compatibility. Re-running this file is a no-op.
INSERT INTO scenes (code, label_zh, label_en, active, sort_order) VALUES
    ('restaurant', '餐厅', 'Restaurant', true, 1),
    ('hotel', '酒店', 'Hotel', true, 2),
    ('taxi', '出租车', 'Taxi', true, 3),
    ('airport', '机场', 'Airport', true, 4),
    ('clinic', '诊所', 'Clinic', true, 5),
    ('tourism_information', '旅游信息', 'Tourism information', true, 6),
    ('emergencies', '紧急情况', 'Emergencies', true, 7),
    ('spoken_languages', '口语', 'Spoken languages', true, 8),
    ('business_negotiation', '商务谈判', 'Business negotiation', true, 9),
    ('shopping', '购物', 'Shopping', true, 10)
ON CONFLICT (code) DO UPDATE SET
    label_zh = EXCLUDED.label_zh,
    label_en = EXCLUDED.label_en,
    active = EXCLUDED.active,
    sort_order = EXCLUDED.sort_order;
