#!/usr/bin/env bash
set -euo pipefail

# One-time, non-destructive host bootstrap. It never stops the legacy site and
# never imports legacy JSON. Run as root and point it at the checked-out new
# platform while production still serves the old branch.

PROJECT_DIR=${1:-/home/cjg/annotation_tool}
PYTHON="$PROJECT_DIR/.venv/bin/python"
MANAGE="$PROJECT_DIR/manage_state.py"
ADMIN_ENV=/etc/annotation-tool-admin.env
APP_ENV=/etc/annotation-tool.env
STAGING_ENV=/etc/annotation-tool-staging.env
PG_CONF=/etc/postgresql/16/main/conf.d/annotation-tool.conf

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "run as root: sudo $0 [new-platform-directory]" >&2
  exit 1
fi

for command in runuser psql pg_isready openssl systemctl install find chgrp chmod getent groupadd usermod; do
  command -v "$command" >/dev/null || {
    echo "missing command: $command" >&2
    exit 1
  }
done
[ -x "$PYTHON" ] || { echo "missing Python environment: $PYTHON" >&2; exit 1; }
[ -f "$MANAGE" ] || { echo "missing migration tool: $MANAGE" >&2; exit 1; }
[ -d /home/ck/ar_audios ] || { echo "missing audio directory" >&2; exit 1; }

install -d -o postgres -g postgres -m 0750 "$(dirname "$PG_CONF")"
install -o postgres -g postgres -m 0644 /dev/null "$PG_CONF"
cat > "$PG_CONF" <<'EOF'
# Annotation platform: 64 maximum application connections plus admin headroom.
max_connections = 150
shared_buffers = '8GB'
effective_cache_size = '32GB'
maintenance_work_mem = '1GB'
work_mem = '16MB'
password_encryption = 'scram-sha-256'
EOF
systemctl restart postgresql
pg_isready -h 127.0.0.1 -p 5432 -t 10 >/dev/null

if [ -r "$ADMIN_ENV" ]; then
  # shellcheck disable=SC1090
  source "$ADMIN_ENV"
else
  existing=$(
    runuser -u postgres -- psql -X -Atqc \
      "SELECT 'role:' || rolname FROM pg_roles
         WHERE rolname IN ('annotation_owner','annotation_app','annotation_backup')
       UNION ALL
       SELECT 'db:' || datname FROM pg_database
         WHERE datname IN ('annotation_tool','annotation_tool_staging')"
  )
  if [ -n "$existing" ]; then
    echo "refusing to adopt pre-existing annotation roles/databases without $ADMIN_ENV" >&2
    printf '%s\n' "$existing" >&2
    exit 1
  fi

  ANNOTATION_OWNER_PASSWORD=$(openssl rand -hex 32)
  ANNOTATION_APP_PASSWORD=$(openssl rand -hex 32)
  ANNOTATION_BACKUP_PASSWORD=$(openssl rand -hex 32)
  ANNOTATION_OWNER_DSN="postgresql://annotation_owner:${ANNOTATION_OWNER_PASSWORD}@127.0.0.1:5432/annotation_tool"
  ANNOTATION_STAGING_OWNER_DSN="postgresql://annotation_owner:${ANNOTATION_OWNER_PASSWORD}@127.0.0.1:5432/annotation_tool_staging"
  ANNOTATION_OWNER_ADMIN_DSN="postgresql://annotation_owner:${ANNOTATION_OWNER_PASSWORD}@127.0.0.1:5432/postgres"
  ANNOTATION_APP_DSN="postgresql://annotation_app:${ANNOTATION_APP_PASSWORD}@127.0.0.1:5432/annotation_tool"
  ANNOTATION_STAGING_APP_DSN="postgresql://annotation_app:${ANNOTATION_APP_PASSWORD}@127.0.0.1:5432/annotation_tool_staging"
  ANNOTATION_BACKUP_DSN="postgresql://annotation_backup:${ANNOTATION_BACKUP_PASSWORD}@127.0.0.1:5432/annotation_tool"

  install -o root -g huawei -m 0640 /dev/null "$ADMIN_ENV"
  {
    printf "ANNOTATION_OWNER_PASSWORD='%s'\n" "$ANNOTATION_OWNER_PASSWORD"
    printf "ANNOTATION_APP_PASSWORD='%s'\n" "$ANNOTATION_APP_PASSWORD"
    printf "ANNOTATION_BACKUP_PASSWORD='%s'\n" "$ANNOTATION_BACKUP_PASSWORD"
    printf "ANNOTATION_OWNER_DSN='%s'\n" "$ANNOTATION_OWNER_DSN"
    printf "ANNOTATION_STAGING_OWNER_DSN='%s'\n" "$ANNOTATION_STAGING_OWNER_DSN"
    printf "ANNOTATION_OWNER_ADMIN_DSN='%s'\n" "$ANNOTATION_OWNER_ADMIN_DSN"
    printf "ANNOTATION_APP_DSN='%s'\n" "$ANNOTATION_APP_DSN"
    printf "ANNOTATION_STAGING_APP_DSN='%s'\n" "$ANNOTATION_STAGING_APP_DSN"
    printf "ANNOTATION_BACKUP_DSN='%s'\n" "$ANNOTATION_BACKUP_DSN"
  } > "$ADMIN_ENV"
fi

: "${ANNOTATION_OWNER_PASSWORD:?missing owner password}"
: "${ANNOTATION_APP_PASSWORD:?missing app password}"
: "${ANNOTATION_BACKUP_PASSWORD:?missing backup password}"
: "${ANNOTATION_OWNER_DSN:?missing production owner DSN}"
: "${ANNOTATION_STAGING_OWNER_DSN:?missing staging owner DSN}"
: "${ANNOTATION_OWNER_ADMIN_DSN:?missing owner admin DSN}"
: "${ANNOTATION_APP_DSN:?missing production app DSN}"
: "${ANNOTATION_STAGING_APP_DSN:?missing staging app DSN}"
: "${ANNOTATION_BACKUP_DSN:?missing backup DSN}"

runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 \
  --set=owner_pw="$ANNOTATION_OWNER_PASSWORD" \
  --set=app_pw="$ANNOTATION_APP_PASSWORD" \
  --set=backup_pw="$ANNOTATION_BACKUP_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE annotation_owner LOGIN CREATEDB PASSWORD %L', :'owner_pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annotation_owner') \gexec
SELECT format('ALTER ROLE annotation_owner WITH LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'owner_pw') \gexec

SELECT format('CREATE ROLE annotation_app LOGIN PASSWORD %L', :'app_pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annotation_app') \gexec
SELECT format('ALTER ROLE annotation_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'app_pw') \gexec

SELECT format('CREATE ROLE annotation_backup LOGIN PASSWORD %L', :'backup_pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'annotation_backup') \gexec
SELECT format('ALTER ROLE annotation_backup WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'backup_pw') \gexec

SELECT 'CREATE DATABASE annotation_tool OWNER annotation_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'annotation_tool') \gexec
SELECT 'CREATE DATABASE annotation_tool_staging OWNER annotation_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'annotation_tool_staging') \gexec
ALTER DATABASE annotation_tool OWNER TO annotation_owner;
ALTER DATABASE annotation_tool_staging OWNER TO annotation_owner;
SQL

for database in annotation_tool annotation_tool_staging; do
  runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 --dbname="$database" <<SQL
ALTER SCHEMA public OWNER TO annotation_owner;
GRANT CONNECT ON DATABASE $database TO annotation_app, annotation_backup;
GRANT USAGE ON SCHEMA public TO annotation_app, annotation_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO annotation_app;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO annotation_app;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO annotation_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO annotation_backup;
SQL
done

PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_OWNER_DSN" \
  "$PYTHON" "$MANAGE" apply-migrations
PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_STAGING_OWNER_DSN" \
  "$PYTHON" "$MANAGE" apply-migrations

for database in annotation_tool annotation_tool_staging; do
  runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 --dbname="$database" <<SQL
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO annotation_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO annotation_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO annotation_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO annotation_backup;
SQL
done

install -o root -g root -m 0600 /dev/null "$APP_ENV"
printf "ANNOTATION_DB_DSN='%s'\n" "$ANNOTATION_APP_DSN" > "$APP_ENV"
install -o root -g huawei -m 0640 /dev/null "$STAGING_ENV"
printf "ANNOTATION_DB_DSN='%s'\n" "$ANNOTATION_STAGING_APP_DSN" > "$STAGING_ENV"

PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_APP_DSN" \
  "$PYTHON" "$MANAGE" schema
PYTHONDONTWRITEBYTECODE=1 ANNOTATION_DB_DSN="$ANNOTATION_STAGING_APP_DSN" \
  "$PYTHON" "$MANAGE" schema

if ! getent group annotation-audio >/dev/null; then
  groupadd --system annotation-audio
fi
usermod -aG annotation-audio www-data
usermod -aG annotation-audio huawei
chgrp -R annotation-audio /home/ck/ar_audios
find /home/ck/ar_audios -type d -exec chmod g+rx,g+s {} +
find /home/ck/ar_audios -type f -exec chmod g+r {} +
systemctl restart nginx

sample=$(find /home/ck/ar_audios -type f -print -quit)
[ -n "$sample" ] || { echo "audio directory contains no files" >&2; exit 1; }
runuser -u www-data -- test -r "$sample"

echo "PostgreSQL production/staging bootstrap complete"
echo "Production database remains empty; the legacy website was not stopped"
