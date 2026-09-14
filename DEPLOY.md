# 部署与迁移指南（PostgreSQL 架构）

## 目标架构

```text
Cloudflare Tunnel → Nginx 127.0.0.1:8080
                        ├─ / → Gunicorn 127.0.0.1:8081 → Flask → PostgreSQL
                        └─ internal audio alias ← X-Accel-Redirect
```

当前生产 `main` 在切换前保持不变，并作为旧 JSON 版本的回滚指针保留。所有开发位于 `codex/postgres-annotation-platform`；迁移全量校验通过后，把生产 worktree 切到该分支，不移动 `main`。

## 1. 前置条件

- 当前站点无人使用。
- `codex/postgres-annotation-platform` 的 `uv run pytest -q` 全部通过。
- 至少有：
  - 生产 JSON 完整快照；
  - 迁移 manifest；
  - PostgreSQL 备份目标；
  - 与系统盘不同的异盘/异机备份位置。
- 本机观测资源：32 CPU threads、64 GiB RAM、约 1 TiB 空闲 NVMe（切换前重新确认）。

## 2. 安装系统服务

以下操作需要 root：

```bash
sudo apt update
sudo apt install -y postgresql-16 postgresql-client-16 nginx
```

安装 uv（以运行服务的 huawei 用户执行）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /home/cjg/annotation_tool
uv python install 3.12
uv sync --frozen --group dev --group preprocess
```

后续所有 Python 命令使用 `uv run`。

## 3. PostgreSQL 角色和数据库

使用三类角色：

- `annotation_owner`：schema owner / migration；
- `annotation_app`：Web/预处理/分类运行时；
- `annotation_backup`：只读备份。

示例（密码请用密码管理器生成）：

```sql
CREATE ROLE annotation_owner LOGIN CREATEDB PASSWORD '...';
CREATE ROLE annotation_app LOGIN PASSWORD '...';
CREATE ROLE annotation_backup LOGIN PASSWORD '...';
CREATE DATABASE annotation_tool OWNER annotation_owner;

\c annotation_tool
GRANT CONNECT ON DATABASE annotation_tool TO annotation_app, annotation_backup;
GRANT USAGE ON SCHEMA public TO annotation_app, annotation_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO annotation_app;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO annotation_app;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO annotation_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE annotation_owner IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO annotation_backup;
```

执行 migration 后，再补现有对象权限：

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO annotation_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO annotation_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO annotation_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO annotation_backup;
```

## 4. PostgreSQL 初始调优

模板：`deploy/postgresql-tuning.conf`。当前计划：

```text
max_connections = 150
shared_buffers = 8GB
effective_cache_size = 32GB
maintenance_work_mem = 1GB
work_mem = 16MB
```

应用池：较大主机可用 4 Gunicorn workers × 每 worker `max_size=16`（理论上限 64）。本仓库 4 核/~8GB 隔离预览与容量测试使用 **2 workers × 4 threads**（`GUNICORN_WORKERS=2 GUNICORN_THREADS=4`）。至少 50 个连接留给迁移、预处理、备份、管理和监控。

```bash
sudo systemctl restart postgresql
sudo -u postgres psql -c 'SHOW max_connections'
```

只提高连接数并不等于提高吞吐。上线后监控连接内存、池等待和慢 SQL；进一步增长时优先评估 PgBouncer。

## 5. 环境文件

```bash
sudo cp deploy/annotation-tool.env.example /etc/annotation-tool.env
sudo chown root:huawei /etc/annotation-tool.env
sudo chmod 640 /etc/annotation-tool.env
sudo editor /etc/annotation-tool.env
```

Admin Dashboard 使用独立高熵密钥。生成一次明文密钥和对应摘要：

```bash
uv run python -c 'import hashlib,secrets; k=secrets.token_urlsafe(32); print("ADMIN KEY (store in password manager):", k); print("ANNOTATION_ADMIN_KEY_SHA256=" + hashlib.sha256(k.encode()).hexdigest())'
```

只将 SHA-256 摘要写入 `/etc/annotation-tool.env`，明文存入团队密码管理器。
生产必须设置 `ANNOTATION_ADMIN_COOKIE_SECURE=true`，并通过 HTTPS 访问 `/admin`。
`ANNOTATION_ADMIN_TRUSTED_PROXIES` 只填写直接连接应用的可信反向代理；默认仅
`127.0.0.1,::1`。Nginx 模板会覆盖客户端传入的 `X-Forwarded-For`，避免伪造
来源地址绕过 Admin 登录限流，并对 `/api/admin/login` 设置独立共享限速区；上线前
应通过 `nginx -t` 校验并 reload 配置。
轮换密钥时更新摘要和 `ANNOTATION_ADMIN_KEY_ID`，重启服务后现有 Admin Session
仍可在其短 TTL 内存在；需要立即吊销时清理 `admin_sessions` 活跃记录。

migration 使用 owner DSN 临时导出到当前 shell，不写入仓库：

```bash
# DSN without a password; libpq reads PGPASSWORD, ~/.pgpass, or PGSERVICEFILE.
export ANNOTATION_DB_DSN='postgresql://annotation_owner@127.0.0.1:5432/annotation_tool'
uv run python manage_state.py apply-migrations
uv run python manage_state.py schema
```

## 6. Nginx 和音频 Range

```bash
sudo cp deploy/nginx-annotation.conf /etc/nginx/sites-available/annotation-tool
sudo ln -sfn /etc/nginx/sites-available/annotation-tool /etc/nginx/sites-enabled/annotation-tool
sudo nginx -t
sudo systemctl reload nginx
```

Ubuntu 默认 Unix socket 使用 peer 认证，因此应用、迁移和备份 DSN 统一使用
`127.0.0.1:5432` 的 SCRAM 密码认证。不要把密码角色的 DSN 改回
`host=/var/run/postgresql`，除非已经为这些角色配置并验证了精确的本地认证规则。

Nginx worker 使用 `www-data`，需要通过专用只读组访问音频，不能加入权限过大的
`huawei` 组：

```bash
sudo groupadd --system --force annotation-audio
sudo usermod -aG annotation-audio www-data
sudo usermod -aG annotation-audio huawei
sudo chgrp -R annotation-audio /home/ck/ar_audios
sudo find /home/ck/ar_audios -type d -exec chmod g+rXs {} +
sudo find /home/ck/ar_audios -type f -exec chmod g+r {} +
```

`/_protected_audio/` 是 `internal` location：浏览器不能直接访问。Flask 验证 session 和路径后返回 `X-Accel-Redirect`，Nginx 使用 sendfile 和 Range 传音频，不占 Gunicorn thread。

Cloudflare `~/.cloudflared/config.yml` origin 指向：

```yaml
ingress:
  - hostname: arabic-annotation.top
    service: http://127.0.0.1:8080
  - service: http_status:404
```

隧道服务模板固定 HTTP/2，避免当前环境的 QUIC 超时：`cloudflared-tunnel.service`。

## 7. 冻结与快照

当前无人使用，直接停止旧服务：

```bash
sudo systemctl stop cloudflared-tunnel audio-annotator
```

制作只读快照，不修改原目录：

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo mkdir -p /home/ck/migration/$stamp
sudo rsync -aHAX --numeric-ids /home/ck/annotations/ /home/ck/migration/$stamp/annotations/
sudo rsync -aHAX --numeric-ids /home/ck/annotations_copy/ /home/ck/migration/$stamp/annotations_copy/
cp config.json /home/ck/migration/$stamp/config.json
cp -a .git /home/ck/migration/$stamp/repo-git
find /home/ck/migration/$stamp/annotations -type f -print0 | sort -z | xargs -0 sha256sum \
  > /home/ck/migration/$stamp/annotations.SHA256SUMS
chmod -R a-w /home/ck/migration/$stamp
```

## 8. 只读盘点

```bash
export ANNOTATION_DB_DSN='postgresql://annotation_owner:...@127.0.0.1:5432/annotation_tool'
uv run python manage_state.py inspect-json \
  --annotations /home/ck/migration/$stamp/annotations \
  --audio /home/ck/ar_audios \
  --output /home/ck/migration/$stamp/manifest.json
```

非零退出表示阻断。必须先解决：

- 损坏/非对象 JSON；
- 旧聚合 JSON；
- 同 stem 不同扩展或 `|/-` legacy key 碰撞；
- 缺音频/无法映射 rel_path；
- completed 状态仍有 assignment；
- 同一任务分给多人；
- segment ID 重复或时间非法。

禁止跳过错误继续导入。

## 9. 导入与全量校验

```bash
uv run python manage_state.py migrate-json \
  --manifest /home/ck/migration/$stamp/manifest.json

uv run python manage_state.py verify-json \
  --manifest /home/ck/migration/$stamp/manifest.json \
  --output /home/ck/migration/$stamp/verification.json
```

`verify-json` 会从 PostgreSQL 重建每条规范 JSON，比较 semantic SHA，并核对 assignments。只有 `ok=true` 且 mismatches=0 才能切换。

再输出兼容快照做第二次恢复格式：

```bash
uv run python manage_state.py export-json \
  --output /home/ck/migration/$stamp/postgres-roundtrip-json
```

## 10. 切换生产 worktree（保留 main）

```bash
git -C /home/cjg/annotation_tool/.worktrees/postgres-platform status --short
git -C /home/cjg/annotation_tool/.worktrees/postgres-platform switch --detach

cd /home/cjg/annotation_tool
git status --short                  # 必须确认没有会冲突的未知修改
git switch codex/postgres-annotation-platform
```

关联开发 worktree 必须先 detach，否则 Git 会拒绝在生产 worktree 检出同一分支。不要移动或删除 `main`，也不要在生产分支追加未经验证的临时补丁。

同步锁定依赖：

```bash
uv sync --frozen --group dev --group preprocess
```

## 11. 安装 systemd 资产

```bash
sudo cp audio-annotator.service /etc/systemd/system/audio-annotator.service
sudo cp cloudflared-tunnel.service /etc/systemd/system/cloudflared-tunnel.service
sudo cp deploy/annotation-backup.service /etc/systemd/system/
sudo cp deploy/annotation-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable postgresql nginx audio-annotator cloudflared-tunnel annotation-backup.timer
```

启动顺序：

```bash
sudo systemctl start postgresql nginx
sudo systemctl start audio-annotator
curl -fsS http://127.0.0.1:8080/api/health
sudo systemctl start cloudflared-tunnel annotation-backup.timer
curl -fsS https://arabic-annotation.top/api/health
```

## 12. Smoke test

用两个测试账号验证：

1. 登录后无任务时显示 Claim next task；
2. 两个账号不能拿到同一任务；
3. 输入后显示 Unsaved → Saving → Saved；
4. Logout 再登录恢复同一任务；
5. Mark done 后进入 idle，不自动领取；
6. Completed data 仅显示本人；
7. 有任务时 Correct 禁用；无任务时可进入 revision；
8. Previous completed / Newer / Older 不改变当前 assignment；
9. 音频请求返回 206 和 Content-Range；
10. 排行榜显示 `xx h xx min`。

## 13. 备份与恢复验证

```bash
sudo cp deploy/backup.env.example /etc/annotation-tool-backup.env
sudo chown root:huawei /etc/annotation-tool-backup.env
sudo chmod 640 /etc/annotation-tool-backup.env
sudo editor /etc/annotation-tool-backup.env
sudo systemctl start annotation-backup.service
sudo journalctl -u annotation-backup.service -n 50 --no-pager
```

每次备份包含：

- PostgreSQL custom dump；
- 从该 dump 恢复到临时库后再导出的兼容 JSON（保证两个介质同一恢复点）；
- restore list、schema 版本和 SHA-256；
- 配置 `ANNOTATION_BACKUP_REMOTE_DIR` 时，复制到不同物理盘/异机路径。

`ANNOTATION_ADMIN_DSN` 可以留空；脚本会启动仅监听临时 Unix socket 的隔离
PostgreSQL 实例完成恢复校验，避免在无人值守备份配置中保存生产管理员密码。
只保存在 `/home/ck` 仍属于同盘备份，不能替代异盘或异机灾备。

恢复演练：

```bash
/home/cjg/annotation_tool/deploy/verify-postgres-backup.sh /home/ck/annotation_db_backups/<timestamp>
```

该脚本在空库执行 pg_restore，核对 task/version/segment/waveform/assignment 行数、sequence 可读性，以及 dump 内 JSON 与恢复后再导出 JSON 的 semantic manifest。

## 14. 监控

`/api/health` 同时检查 Flask 和数据库 schema。`monitor.sh`：

- 本地 8080 失败时先看 PostgreSQL，再处理 audio-annotator；
- 本地正常、公网失败时重启 cloudflared；
- 30 分钟最多 5 次，最小间隔 90 秒。

sudoers 需允许三个服务：

```bash
echo 'huawei ALL=(root) NOPASSWD: /usr/bin/systemctl * audio-annotator, /usr/bin/systemctl * cloudflared-tunnel, /usr/bin/systemctl * postgresql' \
  | sudo tee /etc/sudoers.d/annotator-monitor
sudo chmod 440 /etc/sudoers.d/annotator-monitor
sudo visudo -c
```

## 15. 回滚

### 新平台尚无新标注

```bash
sudo systemctl stop cloudflared-tunnel audio-annotator
cd /home/cjg/annotation_tool
git switch main
# 恢复切换前 service/config 和只读 JSON 快照，再启动旧服务
```

`main` 始终保留在切换前的旧版提交，因此回滚不需要改写任何分支历史。

### 已产生新标注

先停止写入，再导出最新兼容 JSON：

```bash
uv run python manage_state.py export-json --output /home/ck/rollback-json-$(date +%Y%m%d-%H%M%S)
```

核对导出后，旧应用指向新目录；不能覆盖迁移前快照。PostgreSQL、迁移前 JSON 和回滚导出全部保留审计。

## 16. 场景元数据导出/导入与兼容回退

**Checking out git `5567209` after migration 003 is not schema rollback.**
`assert_schema_current()` requires the running build to ship every applied
migration file. The compatibility path is the **same new-schema build** with
feature flags off.

Placeholders only; never point these at a live staging DSN:

```bash
export ANNOTATION_DB_DSN='$ANNOTATION_DB_DSN'
export TARGET_DB_DSN='$TARGET_DB_DSN'
export BACKUP_DUMP='$BACKUP_DIR/annotation_tool.dump'
export PG_BINDIR='/usr/lib/postgresql/16/bin'   # or /usr/lib/postgresql/18/bin
export METADATA_DIR='$BACKUP_DIR/metadata'
export MAPPING_JSON='$BACKUP_DIR/identity-mapping.json'
```

Feature flags (scope enforcement stays on even with fifo):

```bash
export ANNOTATION_METADATA_UI=0
export ANNOTATION_METADATA_WRITE=0
export ANNOTATION_SCENE_REVIEW_WRITE=0
export ANNOTATION_CLAIM_POLICY=fifo
# Then start the same migrated build and check /api/health.
```

PostgreSQL custom dump is the complete backup (tasks, versions, segments,
assignments, provenance, reviews, audit). Metadata JSON is a sidecar round
trip, not a substitute:

```bash
uv run python manage_state.py dump-postgres --output "$BACKUP_DUMP" --pg-bindir "$PG_BINDIR"
# Restore only into a newly created database with no user schema objects
# (any non-system table/view/sequence, not merely annotation_tasks rows).
# Pass --target-dsn without a password; libpq reads PGPASSWORD, ~/.pgpass,
# or PGSERVICE/PGSERVICEFILE.
createdb --maintenance-db="$ADMIN_DSN" new_annotation_restore
uv run python manage_state.py restore-postgres \
  --dump "$BACKUP_DUMP" --target-dsn "$TARGET_DB_DSN" --pg-bindir "$PG_BINDIR"
```

Versioned metadata (exact task/version/user IDs, or an explicit UUID mapping
file; never username or pathname remaps):

```bash
uv run python manage_state.py export-metadata --output "$METADATA_DIR"
uv run python manage_state.py verify-metadata --input "$METADATA_DIR"
uv run python manage_state.py import-metadata --input "$METADATA_DIR" --dry-run
uv run python manage_state.py import-metadata --input "$METADATA_DIR"
# Optional explicit mapping + scope replace:
uv run python manage_state.py import-metadata \
  --input "$METADATA_DIR" --mapping "$MAPPING_JSON" --replace-scopes
```

Verify a restore by comparing task/version/segment/assignment/source/review
counts and relationships before declaring the target usable. Real production
data migration remains deferred.

Legacy `export-json` field contract is unchanged. Training `data.json` is
unchanged; scene evidence is `scene_metadata.json` (audio-level, not segment
labels). Excel adds source scene/confidence/batch and human verification columns.

## 常用命令

```bash
uv run python manage_state.py schema
uv run python manage_state.py assignments-list
uv run python manage_state.py assignment-release --username alice --reason 'confirmed by coordinator'
uv run python export.py --output annotation_export.xlsx
uv run pytest -q

sudo systemctl status postgresql nginx audio-annotator cloudflared-tunnel annotation-backup.timer
sudo journalctl -u postgresql -u nginx -u audio-annotator -u cloudflared-tunnel -n 100 --no-pager
```
