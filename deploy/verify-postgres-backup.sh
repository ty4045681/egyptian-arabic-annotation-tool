#!/usr/bin/env bash
set -euo pipefail

backup=${1:?usage: verify-postgres-backup.sh /path/to/backup-dir}
ENV_FILE=${ANNOTATION_BACKUP_ENV_FILE:-/etc/annotation-tool-backup.env}
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${ANNOTATION_ADMIN_DSN:?}"
PROJECT_DIR=/home/cjg/annotation_tool
cd "$backup"
sha256sum --check SHA256SUMS
pg_restore --list annotation_tool.dump >/dev/null

restore_db="annotation_restore_check_$(date +%s)_$$"
temp=$(mktemp -d)
cleanup(){
  rm -rf "$temp"
  dropdb --if-exists --maintenance-db="$ANNOTATION_ADMIN_DSN" "$restore_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT

createdb --maintenance-db="$ANNOTATION_ADMIN_DSN" "$restore_db"
restore_dsn=$(
  ANNOTATION_ADMIN_DSN="$ANNOTATION_ADMIN_DSN" RESTORE_DB="$restore_db" \
    /home/huawei/.local/bin/uv run --directory "$PROJECT_DIR" python - <<'PY'
import os
from psycopg.conninfo import make_conninfo
print(make_conninfo(os.environ["ANNOTATION_ADMIN_DSN"], dbname=os.environ["RESTORE_DB"]))
PY
)
pg_restore --dbname="$restore_dsn" --no-owner --no-acl annotation_tool.dump

export ANNOTATION_DB_DSN="$restore_dsn"
/home/huawei/.local/bin/uv run --directory "$PROJECT_DIR" python manage_state.py schema >/dev/null
/home/huawei/.local/bin/uv run --directory "$PROJECT_DIR" python manage_state.py export-json \
  --output "$temp/restored-json"

psql --dbname="$restore_dsn" --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command "SELECT json_build_object(
      'tasks', (SELECT count(*) FROM annotation_tasks),
      'versions', (SELECT count(*) FROM annotation_versions),
      'segments', (SELECT count(*) FROM segments),
      'waveforms', (SELECT count(*) FROM waveforms),
      'assignments', (SELECT count(*) FROM assignments)
    );" > "$temp/restored-counts.json"

/home/huawei/.local/bin/uv run --directory "$PROJECT_DIR" python - \
  database-counts.json "$temp/restored-counts.json" \
  "$temp/restored-json/export_manifest.json" <<'PY'
import json, sys, tarfile, tempfile
from pathlib import Path

expected_counts = json.loads(Path(sys.argv[1]).read_text())
actual_counts = json.loads(Path(sys.argv[2]).read_text())
if expected_counts != actual_counts:
    raise SystemExit(f"database count mismatch: {expected_counts} != {actual_counts}")

restored_manifest = json.loads(Path(sys.argv[3]).read_text())
with tempfile.TemporaryDirectory() as directory:
    with tarfile.open("legacy-json.tar.gz", "r:gz") as archive:
        archive.extract("legacy-json/export_manifest.json", directory)
    backup_manifest = json.loads(
        (Path(directory) / "legacy-json" / "export_manifest.json").read_text()
    )
if backup_manifest != restored_manifest:
    raise SystemExit("compatibility JSON semantic manifest mismatch")
PY

psql --dbname="$restore_dsn" --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --command "SELECT nextval('task_allocation_order_seq');
             SELECT nextval('annotation_events_id_seq');" >/dev/null

dropdb --maintenance-db="$ANNOTATION_ADMIN_DSN" "$restore_db"
trap - EXIT
rm -rf "$temp"
echo "restore verification passed: $backup"
