#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT_DIR=/home/cjg/annotation_tool
ENV_FILE=${ANNOTATION_BACKUP_ENV_FILE:-/etc/annotation-tool-backup.env}
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${ANNOTATION_BACKUP_DIR:?}"
: "${ANNOTATION_BACKUP_RETENTION_DAYS:=14}"
: "${ANNOTATION_DB_DSN:?}"
ANNOTATION_BACKUP_REMOTE_DIR=${ANNOTATION_BACKUP_REMOTE_DIR:-}
ANNOTATION_ADMIN_DSN=${ANNOTATION_ADMIN_DSN:-}

if [ -n "${ANNOTATION_PG_BINDIR:-}" ]; then
  PG_BINDIR=$ANNOTATION_PG_BINDIR
elif [ -d /usr/lib/postgresql/16/bin ]; then
  # Production is PostgreSQL 16. Prefer its versioned directory so a Conda
  # pg_config earlier in PATH cannot select an incomplete client toolchain.
  PG_BINDIR=/usr/lib/postgresql/16/bin
else
  PG_BINDIR=$(pg_config --bindir)
fi
for tool in pg_dump pg_restore createdb dropdb psql initdb pg_ctl; do
  [ -x "$PG_BINDIR/$tool" ] || {
    echo "missing PostgreSQL tool: $PG_BINDIR/$tool" >&2
    exit 1
  }
done

mkdir -p "$ANNOTATION_BACKUP_DIR"
exec 9>"$ANNOTATION_BACKUP_DIR/.backup.lock"
if ! flock -n 9; then
  echo "backup already running; skipped"
  exit 0
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work="$ANNOTATION_BACKUP_DIR/.tmp-$stamp-$$"
final="$ANNOTATION_BACKUP_DIR/$stamp"
restore_db="annotation_backup_build_${stamp//[^0-9A-Za-z]/_}_$$"
restore_admin_dsn=$ANNOTATION_ADMIN_DSN
local_pgdata=
local_socket=

[ ! -e "$final" ] || { echo "backup target already exists: $final" >&2; exit 1; }
mkdir -p "$work"

cleanup(){
  if [ -n "$restore_db" ] && [ -n "$restore_admin_dsn" ]; then
    "$PG_BINDIR/dropdb" --if-exists \
      --maintenance-db="$restore_admin_dsn" "$restore_db" >/dev/null 2>&1 || true
  fi
  if [ -n "$local_pgdata" ] && [ -s "$local_pgdata/PG_VERSION" ]; then
    "$PG_BINDIR/pg_ctl" -D "$local_pgdata" -m immediate -w stop >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

"$PG_BINDIR/pg_dump" --dbname="$ANNOTATION_DB_DSN" --format=custom --compress=6 \
  --file="$work/annotation_tool.dump"
"$PG_BINDIR/pg_restore" --list "$work/annotation_tool.dump" > "$work/restore.list"

# A production admin DSN is optional. Without one, start an isolated temporary
# PostgreSQL cluster owned by the backup user and restore into that. This keeps
# production CREATEDB credentials out of unattended backup configuration while
# still proving that every dump can be restored.
if [ -z "$restore_admin_dsn" ]; then
  local_pgdata="$work/.restore-pgdata"
  local_socket="$work/.restore-socket"
  mkdir -p "$local_socket"
  "$PG_BINDIR/initdb" -D "$local_pgdata" --auth=trust --no-locale \
    --encoding=UTF8 > "$work/restore-initdb.log"
  "$PG_BINDIR/pg_ctl" -D "$local_pgdata" -w \
    -l "$work/restore-postgres.log" \
    -o "-F -k $local_socket -c listen_addresses='' -p 5432" start
  restore_admin_dsn="host=$local_socket port=5432 dbname=postgres user=$(id -un)"
fi

"$PG_BINDIR/createdb" --maintenance-db="$restore_admin_dsn" "$restore_db"
restore_dsn=$(
  cd "$PROJECT_DIR"
  ANNOTATION_ADMIN_DSN="$restore_admin_dsn" RESTORE_DB="$restore_db" \
    /home/huawei/.local/bin/uv run python - <<'PY'
import os
from psycopg.conninfo import make_conninfo
print(make_conninfo(os.environ["ANNOTATION_ADMIN_DSN"], dbname=os.environ["RESTORE_DB"]))
PY
)
"$PG_BINDIR/pg_restore" --dbname="$restore_dsn" --no-owner --no-acl \
  "$work/annotation_tool.dump"

export ANNOTATION_DB_DSN="$restore_dsn"
cd "$PROJECT_DIR"
/home/huawei/.local/bin/uv run python manage_state.py schema > "$work/schema.json"
/home/huawei/.local/bin/uv run python manage_state.py export-json \
  --output "$work/legacy-json"

"$PG_BINDIR/psql" --dbname="$restore_dsn" --set ON_ERROR_STOP=1 \
  --tuples-only --no-align \
  --command "SELECT json_build_object(
      'tasks', (SELECT count(*) FROM annotation_tasks),
      'versions', (SELECT count(*) FROM annotation_versions),
      'segments', (SELECT count(*) FROM segments),
      'waveforms', (SELECT count(*) FROM waveforms),
      'assignments', (SELECT count(*) FROM assignments)
    );" > "$work/database-counts.json"

tar -C "$work" -czf "$work/legacy-json.tar.gz" legacy-json
rm -rf "$work/legacy-json"
"$PG_BINDIR/dropdb" --maintenance-db="$restore_admin_dsn" "$restore_db"
restore_db=

if [ -n "$local_pgdata" ]; then
  "$PG_BINDIR/pg_ctl" -D "$local_pgdata" -m fast -w stop
  rm -rf "$local_pgdata" "$local_socket"
  local_pgdata=
  local_socket=
fi

(
  cd "$work"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\0' \
    | sort -z | xargs -0 sha256sum > SHA256SUMS
)

mv "$work" "$final"
trap - EXIT
if [ -n "$ANNOTATION_BACKUP_REMOTE_DIR" ]; then
  mkdir -p "$ANNOTATION_BACKUP_REMOTE_DIR"
  cp -a "$final" "$ANNOTATION_BACKUP_REMOTE_DIR/"
else
  echo "warning: no off-host/off-disk backup target configured" >&2
fi
find "$ANNOTATION_BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d \
  -name '20??????T??????Z' -mtime "+$ANNOTATION_BACKUP_RETENTION_DAYS" \
  -exec rm -rf -- {} +
echo "backup complete: $final"
