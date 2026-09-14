#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/home/cjg/annotation_tool
DEV=$ROOT/.worktrees/postgres-platform
BRANCH=codex/postgres-annotation-platform
ADMIN_ENV=/etc/annotation-tool-admin.env
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MIGRATION_DIR=/home/ck/migration/$STAMP
ROLLBACK_ARMED=false

MODE=${1:-}
if { [ "$MODE" != "--confirm" ] && [ "$MODE" != "--confirm-reset-import" ]; } || [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "usage: sudo $0 --confirm [fresh database]" >&2
  echo "       sudo $0 --confirm-reset-import [replace a verified, not-yet-live import]" >&2
  exit 2
fi

for command in awk chmod cp crontab curl find flock git install jq ln nginx pg_dump pg_restore pgrep psql rm rsync runuser seq sha256sum sleep sort ss systemctl tee unlink visudo xargs; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done
[ -r "$ADMIN_ENV" ] || { echo "missing $ADMIN_ENV" >&2; exit 1; }
[ -x "$DEV/.venv/bin/python" ] || { echo "missing new-platform .venv" >&2; exit 1; }
[ -f "$DEV/manage_state.py" ] || { echo "missing migration tool" >&2; exit 1; }

# shellcheck disable=SC1090
source "$ADMIN_ENV"
: "${ANNOTATION_OWNER_DSN:?}"
: "${ANNOTATION_OWNER_PASSWORD:?}"
: "${ANNOTATION_BACKUP_PASSWORD:?}"

if [ "$(runuser -u huawei -- git -C "$DEV" branch --show-current)" != "$BRANCH" ]; then
  echo "development worktree is not on $BRANCH" >&2
  exit 1
fi
[ -z "$(runuser -u huawei -- git -C "$DEV" status --porcelain --untracked-files=no)" ] || {
  echo "development worktree has tracked changes" >&2
  exit 1
}
if [ "$(runuser -u huawei -- git -C "$ROOT" branch --show-current)" != main ]; then
  echo "production worktree is not on legacy main" >&2
  exit 1
fi

export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=annotation_owner
export PGPASSWORD=$ANNOTATION_OWNER_PASSWORD PGDATABASE=annotation_tool
business_rows=$(psql -X -At -v ON_ERROR_STOP=1 -c \
  "SELECT (SELECT count(*) FROM annotation_tasks) + (SELECT count(*) FROM import_batches)")
RESET_IMPORTED_DB=false
if [ "$business_rows" != 0 ]; then
  [ "$MODE" = "--confirm-reset-import" ] || {
    echo "production database contains an earlier import; use --confirm-reset-import only after verification" >&2
    exit 1
  }
  IFS='|' read -r existing_tasks completed_batches other_batches active_sessions <<< "$(
    psql -X -At -F '|' -v ON_ERROR_STOP=1 -c \
      "SELECT (SELECT count(*) FROM annotation_tasks),
              (SELECT count(*) FROM import_batches WHERE status='completed'),
              (SELECT count(*) FROM import_batches WHERE status<>'completed'),
              (SELECT count(*) FROM active_sessions)"
  )"
  if [ "$existing_tasks" -le 0 ] || [ "$completed_batches" != 1 ] || [ "$other_batches" != 0 ] || [ "$active_sessions" != 0 ]; then
    echo "existing database is not a safe, completed offline import; refusing reset" >&2
    exit 1
  fi
  RESET_IMPORTED_DB=true
fi

install -d -o root -g huawei -m 0750 "$MIGRATION_DIR"
crontab -u huawei -l > "$MIGRATION_DIR/huawei.crontab.before" 2>/dev/null || :
awk '!/annotation_tool\/(monitor|health_check)\.sh/' \
  "$MIGRATION_DIR/huawei.crontab.before" > "$MIGRATION_DIR/huawei.crontab.maintenance"
crontab -u huawei "$MIGRATION_DIR/huawei.crontab.maintenance"

exec 8>"$ROOT/.monitor.lock"
flock 8

cp -a "$ROOT/config.json" "$MIGRATION_DIR/config.before.json"
cp -a "$ROOT/.git" "$MIGRATION_DIR/repo-git.before"
cp -a /etc/systemd/system/audio-annotator.service "$MIGRATION_DIR/audio-annotator.service.before"
cp -a /etc/systemd/system/cloudflared-tunnel.service "$MIGRATION_DIR/cloudflared-tunnel.service.before"
install -d -m 0750 "$MIGRATION_DIR/nginx-sites-enabled.before"
cp -a /etc/nginx/sites-enabled/. "$MIGRATION_DIR/nginx-sites-enabled.before/"

rollback(){
  rc=$?
  if [ "$rc" -eq 0 ] || ! $ROLLBACK_ARMED; then return; fi
  trap - EXIT INT TERM
  set +e
  echo "cutover failed; restoring legacy service" >&2
  systemctl stop cloudflared-tunnel audio-annotator
  if [ "$(runuser -u huawei -- git -C "$ROOT" branch --show-current 2>/dev/null)" = "$BRANCH" ]; then
    runuser -u huawei -- git -C "$ROOT" switch main
  fi
  if [ -f "$MIGRATION_DIR/config.before.json" ]; then
    install -o huawei -g huawei -m 0600 "$MIGRATION_DIR/config.before.json" "$ROOT/config.json"
  fi
  cp -a "$MIGRATION_DIR/audio-annotator.service.before" /etc/systemd/system/audio-annotator.service
  cp -a "$MIGRATION_DIR/cloudflared-tunnel.service.before" /etc/systemd/system/cloudflared-tunnel.service
  find /etc/nginx/sites-enabled -mindepth 1 -maxdepth 1 -exec rm -f -- {} +
  cp -a "$MIGRATION_DIR/nginx-sites-enabled.before/." /etc/nginx/sites-enabled/
  systemctl daemon-reload
  nginx -t
  systemctl restart postgresql nginx audio-annotator cloudflared-tunnel
  crontab -u huawei "$MIGRATION_DIR/huawei.crontab.before"
  if [ -z "$(runuser -u huawei -- git -C "$DEV" branch --show-current 2>/dev/null)" ]; then
    runuser -u huawei -- git -C "$DEV" switch "$BRANCH"
  fi
  flock -u 8
  echo "legacy service restored; inspect $MIGRATION_DIR" >&2
  exit "$rc"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ROLLBACK_ARMED=true

systemctl stop cloudflared-tunnel
sleep 3
systemctl stop audio-annotator
if systemctl is-active --quiet cloudflared-tunnel || systemctl is-active --quiet audio-annotator; then
  echo "legacy services did not stop" >&2
  exit 1
fi
if pgrep -af '[p]reprocess.py|[c]lassify.py'; then
  echo "preprocess/classify is still writing" >&2
  exit 1
fi

rsync -aHAX --numeric-ids /home/ck/annotations/ "$MIGRATION_DIR/annotations/"
rsync -aHAX --numeric-ids /home/ck/annotations_copy/ "$MIGRATION_DIR/annotations_copy/"
rsync -aHAXnci --delete /home/ck/annotations/ "$MIGRATION_DIR/annotations/" \
  > "$MIGRATION_DIR/source-vs-snapshot.diff"
[ ! -s "$MIGRATION_DIR/source-vs-snapshot.diff" ] || {
  echo "source changed while snapshotting" >&2
  exit 1
}
(
  cd "$MIGRATION_DIR/annotations"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
) > "$MIGRATION_DIR/annotations.SHA256SUMS"
chmod -R a-w "$MIGRATION_DIR/annotations" "$MIGRATION_DIR/annotations_copy"

PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_OWNER_DSN" \
  "$DEV/.venv/bin/python" "$DEV/manage_state.py" inspect-json \
  --annotations "$MIGRATION_DIR/annotations" \
  --audio /home/ck/ar_audios \
  --output "$MIGRATION_DIR/manifest.json" | tee "$MIGRATION_DIR/inspect.log"
jq -e '.errors == [] and .warnings == [] and .summary.items > 0' \
  "$MIGRATION_DIR/manifest.json" >/dev/null

if $RESET_IMPORTED_DB; then
  psql -X -At -v ON_ERROR_STOP=1 -c "
    SELECT json_build_object(
      'tasks',(SELECT count(*) FROM annotation_tasks),
      'batches',(SELECT count(*) FROM import_batches),
      'sessions',(SELECT count(*) FROM active_sessions)
    );" > "$MIGRATION_DIR/replaced-import-counts.json"
  psql -X -v ON_ERROR_STOP=1 -c "
    TRUNCATE TABLE
      annotation_events, operations, assignments, waveforms, segments,
      annotation_versions, annotation_tasks, active_sessions, annotators,
      import_items, import_batches
    RESTART IDENTITY CASCADE;
    ALTER SEQUENCE task_allocation_order_seq RESTART WITH 1;"
  [ "$(psql -X -At -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM annotation_tasks')" = 0 ]
fi

PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_OWNER_DSN" \
  "$DEV/.venv/bin/python" "$DEV/manage_state.py" migrate-json \
  --manifest "$MIGRATION_DIR/manifest.json" | tee "$MIGRATION_DIR/migrate.log"
PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_OWNER_DSN" \
  "$DEV/.venv/bin/python" "$DEV/manage_state.py" verify-json \
  --manifest "$MIGRATION_DIR/manifest.json" \
  --output "$MIGRATION_DIR/verification.json" | tee "$MIGRATION_DIR/verify.log"
PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_OWNER_DSN" \
  "$DEV/.venv/bin/python" "$DEV/manage_state.py" export-json \
  --output "$MIGRATION_DIR/postgres-roundtrip-json" | tee "$MIGRATION_DIR/export.log"

psql -X -At -v ON_ERROR_STOP=1 -c "
WITH status_counts AS (
  SELECT status, count(*)::int AS n FROM annotation_tasks GROUP BY status
)
SELECT json_build_object(
  'tasks',             (SELECT count(*) FROM annotation_tasks),
  'versions',          (SELECT count(*) FROM annotation_versions),
  'segments',          (SELECT count(*) FROM segments),
  'waveforms',         (SELECT count(*) FROM waveforms),
  'assignments',       (SELECT count(*) FROM assignments),
  'eligible',          (SELECT count(*) FROM annotation_tasks WHERE eligible),
  'import_items',      (SELECT count(*) FROM import_items),
  'completed_batches', (SELECT count(*) FROM import_batches WHERE status='completed'),
  'statuses',          (SELECT json_object_agg(status,n) FROM status_counts)
);" > "$MIGRATION_DIR/database-counts.json"

"$DEV/.venv/bin/python" - \
  "$MIGRATION_DIR/manifest.json" \
  "$MIGRATION_DIR/verification.json" \
  "$MIGRATION_DIR/database-counts.json" \
  "$MIGRATION_DIR/postgres-roundtrip-json/export_manifest.json" <<'PY'
import json, sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
verification = json.loads(Path(sys.argv[2]).read_text())
database = json.loads(Path(sys.argv[3]).read_text())
roundtrip = json.loads(Path(sys.argv[4]).read_text())
summary = manifest["summary"]
expected = {
    "tasks": summary["items"],
    "versions": summary["items"],
    "segments": summary["segments"],
    "waveforms": sum(item["waveform_bytes"] > 0 for item in manifest["items"]),
    "assignments": summary["assignments"],
    "eligible": sum(item["segment_count"] > 0 for item in manifest["items"]),
    "import_items": summary["items"],
    "completed_batches": 1,
    "statuses": summary["statuses"],
}
assert manifest["errors"] == []
assert manifest["warnings"] == []
assert verification["ok"] is True
assert verification["checked"] == summary["items"]
assert verification["mismatches"] == []
assert database == expected, (database, expected)
source = {item["rel_path"]: item["semantic_sha256"] for item in manifest["items"]}
exported = {item["rel_path"]: item["semantic_sha256"] for item in roundtrip["semantic_items"]}
assert source == exported
assert roundtrip["count"] == summary["items"]
print("FINAL MIGRATION VERIFICATION PASSED")
print(json.dumps(expected, ensure_ascii=False, sort_keys=True))
print("manifest_sha256:", manifest["manifest_sha256"])
PY

export PGUSER=annotation_backup PGPASSWORD=$ANNOTATION_BACKUP_PASSWORD
pg_dump --format=custom --compress=6 --file="$MIGRATION_DIR/annotation_tool.dump"
pg_restore --list "$MIGRATION_DIR/annotation_tool.dump" > "$MIGRATION_DIR/restore.list"
sha256sum "$MIGRATION_DIR/annotation_tool.dump" > "$MIGRATION_DIR/annotation_tool.dump.sha256"
export PGUSER=annotation_owner PGPASSWORD=$ANNOTATION_OWNER_PASSWORD

runuser -u huawei -- git -C "$DEV" switch --detach
runuser -u huawei -- git -C "$ROOT" switch "$BRANCH"
runuser -u huawei -- env UV_CACHE_DIR=/tmp/annotation-uv-cache \
  /home/huawei/.local/bin/uv sync --directory "$ROOT" --frozen --group dev --group preprocess

jq '.audio_dir="/home/ck/ar_audios" | .port=8081 |
    .audio_accel_prefix="/_protected_audio" |
    .session_timeout_minutes=(.session_timeout_minutes // 30) |
    del(.annotations_dir,.backup_dir)' \
  "$MIGRATION_DIR/config.before.json" > "$MIGRATION_DIR/config.production.json"
install -o huawei -g huawei -m 0600 "$MIGRATION_DIR/config.production.json" "$ROOT/config.json"

install -o root -g root -m 0644 "$ROOT/audio-annotator.service" /etc/systemd/system/audio-annotator.service
install -o root -g root -m 0644 "$ROOT/cloudflared-tunnel.service" /etc/systemd/system/cloudflared-tunnel.service
install -o root -g root -m 0644 "$ROOT/deploy/annotation-backup.service" /etc/systemd/system/annotation-backup.service
install -o root -g root -m 0644 "$ROOT/deploy/annotation-backup.timer" /etc/systemd/system/annotation-backup.timer
install -o root -g root -m 0644 "$ROOT/deploy/nginx-annotation.conf" /etc/nginx/sites-available/annotation-tool

if [ -L /etc/nginx/sites-enabled/default ]; then
  unlink /etc/nginx/sites-enabled/default
fi
ln -sfn /etc/nginx/sites-available/annotation-tool /etc/nginx/sites-enabled/annotation-tool
nginx -t

cat > /etc/sudoers.d/annotator-monitor <<'EOF'
huawei ALL=(root) NOPASSWD: /usr/bin/systemctl reset-failed audio-annotator, /usr/bin/systemctl restart audio-annotator, /usr/bin/systemctl start audio-annotator, /usr/bin/systemctl reset-failed cloudflared-tunnel, /usr/bin/systemctl restart cloudflared-tunnel, /usr/bin/systemctl start cloudflared-tunnel, /usr/bin/systemctl reset-failed postgresql, /usr/bin/systemctl restart postgresql, /usr/bin/systemctl start postgresql
EOF
chmod 0440 /etc/sudoers.d/annotator-monitor
visudo -cf /etc/sudoers.d/annotator-monitor

systemctl daemon-reload
systemctl enable postgresql nginx audio-annotator cloudflared-tunnel
systemctl start postgresql
systemctl restart audio-annotator

for _ in $(seq 1 20); do
  curl -fsS http://127.0.0.1:8081/api/health > "$MIGRATION_DIR/health-8081.json" && break
  sleep 1
done
jq -e '.ok == true and .schema_versions == [1]' "$MIGRATION_DIR/health-8081.json" >/dev/null
systemctl reload nginx
curl -fsS http://127.0.0.1:8080/api/health > "$MIGRATION_DIR/health-8080.json"
jq -e '.ok == true and .schema_versions == [1]' "$MIGRATION_DIR/health-8080.json" >/dev/null

systemctl restart cloudflared-tunnel
public_ok=false
for _ in $(seq 1 20); do
  if curl -fsS https://arabic-annotation.top/api/health > "$MIGRATION_DIR/health-public.json"; then
    public_ok=true
    break
  fi
  sleep 2
done
$public_ok
jq -e '.ok == true and .schema_versions == [1]' "$MIGRATION_DIR/health-public.json" >/dev/null

awk '!/annotation_tool\/(monitor|health_check)\.sh/' \
  "$MIGRATION_DIR/huawei.crontab.before" > "$MIGRATION_DIR/huawei.crontab.after"
printf '%s\n' '* * * * * /home/cjg/annotation_tool/monitor.sh' >> "$MIGRATION_DIR/huawei.crontab.after"
crontab -u huawei "$MIGRATION_DIR/huawei.crontab.after"

ROLLBACK_ARMED=false
flock -u 8
trap - EXIT INT TERM
echo "CUTOVER COMPLETE"
echo "migration_dir=$MIGRATION_DIR"
cat "$MIGRATION_DIR/database-counts.json"
