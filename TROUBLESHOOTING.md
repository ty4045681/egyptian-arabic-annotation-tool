# 标注平台故障排查（PostgreSQL 架构）

所有 Python 命令都在 `/home/cjg/annotation_tool` 下通过 `uv run` 执行。

## 通用检查

```bash
cd /home/cjg/annotation_tool
curl -i http://127.0.0.1:8080/api/health
sudo systemctl status postgresql nginx audio-annotator cloudflared-tunnel --no-pager
sudo journalctl -u postgresql -u nginx -u audio-annotator -u cloudflared-tunnel -n 100 --no-pager
tail -50 gunicorn.log
tail -30 client_errors.log
```

正常健康响应包含：

```json
{"ok": true, "schema_versions": [1]}
```

## 页面返回 503 / 健康检查失败

1. 检查 PostgreSQL：

```bash
sudo systemctl status postgresql
sudo -u postgres psql -d annotation_tool -c 'select now();'
```

2. 检查 schema：

```bash
source /etc/annotation-tool.env
uv run python manage_state.py schema
```

3. 检查应用：

```bash
curl -i http://127.0.0.1:8081/api/health   # 直连 Gunicorn
curl -i http://127.0.0.1:8080/api/health   # 经 Nginx
```

- 8081 正常、8080 失败：Nginx 配置或进程问题。
- 两者都失败、PostgreSQL 正常：应用/DSN/schema 问题。
- `schema_migrations` 缺版本：停止服务，以 migration owner 执行 `uv run python manage_state.py apply-migrations`；不要让 Gunicorn 自动迁移。

## 登录提示名字已使用

同一用户名只允许一个未过期 Web session。确认原设备已 Logout；必要时等待 30 分钟 session 超时。

assignment 与 session 独立：即使 session 超时，重新用同名登录后仍会恢复原任务。

## 保存状态一直显示 Saving / Save failed

```bash
tail -50 client_errors.log
tail -100 gunicorn.log
sudo -u postgres psql -d annotation_tool -c \
  "select wait_event_type, wait_event, state, query from pg_stat_activity where datname='annotation_tool';"
```

行为说明：

- 自动保存把所有 dirty segments 合并为单请求，同任务最多一个在途请求。
- 网络响应丢失时使用相同 operation ID 重试，服务端返回同一 revision，不会重复写。
- `revision conflict` 表示另一个有效页面已经更新该 draft。页面会停止覆盖，需重新加载。
- `Lease token does not match` 表示页面已陈旧或 assignment 已被管理员明确回收，重新登录/加载。

## Logout 后登录出现另一条任务

这是阻断级异常。正常行为是 assignment 永不因 Logout、30 分钟 session 或 72 小时活动时间自动释放。

```bash
uv run python manage_state.py assignments-list
sudo -u postgres psql -d annotation_tool -c \
  "select u.username,t.rel_path,a.mode,a.assigned_at,a.last_activity_at
   from assignments a join annotators u on u.id=a.user_id
   join annotation_tasks t on t.id=a.task_id order by a.assigned_at;"
```

检查是否有人执行了 `assignment-release`，事件可查：

```sql
select e.created_at,u.username,t.rel_path,e.event_type,e.details
from annotation_events e
left join annotators u on u.id=e.user_id
left join annotation_tasks t on t.id=e.task_id
where e.event_type in ('abandoned','released_admin')
order by e.id desc limit 50;
```

## 需要管理员回收任务

禁止直接 DELETE assignments。使用带 reason 的审计命令：

```bash
uv run python manage_state.py assignment-release \
  --username alice \
  --reason 'annotator confirmed task can be reassigned'
```

纠正任务（`mode=revision`）被回收时，会把未提交的纠正草稿标为 `abandoned`，已发布版本保持不变。

## 完成页看不到记录

完成页只显示当前用户名提交的 current published 版本。确认始终使用同一个名字：

```sql
select u.username,t.status,count(*)
from annotation_tasks t
join annotation_versions v on v.id=t.current_published_version_id
join annotators u on u.id=v.submitted_by_user_id
group by u.username,t.status order by u.username,t.status;
```

## Correct 按钮禁用

当前用户有 active assignment 时，完成页只允许查看，不允许创建 correction draft。回标注页完成或明确 Abandon 当前任务后再纠正。

纠正 draft 的自动保存不会修改 published 版本；只有 Mark done / Skip task 后才原子发布。

## 音频无法加载

```bash
sudo nginx -t
curl -I http://127.0.0.1:8080/api/health
sudo tail -100 /var/log/nginx/error.log
tail -50 tunnel.log
```

音频需要登录 Cookie，直接 curl `/api/audio/...` 会 401。浏览器 DevTools 应看到：

- HTTP 200 或 206；
- `Accept-Ranges: bytes`；
- Range 请求有 `Content-Range`；
- 由 Nginx internal alias 处理。

如果 API 正常但所有音频 404，核对：

- `config.json` 的 `audio_dir`；
- `audio_accel_prefix` 为 `/_protected_audio`；
- Nginx `alias /home/ck/ar_audios/;`；
- Nginx 用户对目录和文件有读取/遍历权限。

## 公网 502 / Cloudflare 1033

```bash
curl -fsS http://127.0.0.1:8080/api/health
sudo systemctl status cloudflared-tunnel
sudo tail -100 /home/cjg/annotation_tool/tunnel.log
```

- 本地正常、公网失败：重启 cloudflared。
- 本地失败：先修 PostgreSQL/Nginx/Gunicorn，不要反复重启隧道。
- 隧道模板固定 `--protocol http2`，避免当前环境已观测到的 QUIC timeout。

## PostgreSQL 连接池耗尽

```sql
select state,count(*) from pg_stat_activity where datname='annotation_tool' group by state;
select pid,now()-xact_start as age,state,wait_event_type,wait_event,query
from pg_stat_activity
where datname='annotation_tool' and xact_start is not null
order by xact_start;
```

配置边界：

- PostgreSQL `max_connections=150`；
- 4 workers × 每池 max 16 = 应用理论上限 64；
- 其余留给迁移、预处理、备份和运维。

不要先盲目提高连接数。优先处理长事务、泄漏、慢 SQL；持续增长时评估 PgBouncer。

## 慢查询或平台卡顿

```sql
select pid,now()-query_start as age,wait_event_type,wait_event,query
from pg_stat_activity
where datname='annotation_tool' and state <> 'idle'
order by query_start;
```

检查：

- `FOR UPDATE SKIP LOCKED` 是否等待异常锁；
- autovacuum 是否积压；
- `log_min_duration_statement=500` 日志；
- 磁盘空间和 iowait；
- Nginx 是否负责音频（Gunicorn 不应持续传大文件）；
- 浏览器是否每任务只有一个保存请求。

## 迁移 verify 失败

不要手工改生产库绕过差异。

1. 查看 verification JSON 的 `semantic_mismatch` / `missing_database_task`。
2. 修 importer/schema。
3. 删除演练数据库并从原只读快照重跑。
4. 只有 mismatches=0 才能切换。

源 JSON 在 manifest 后变化时，`migrate-json` 会立即中止。

## 备份失败或恢复验证失败

```bash
sudo systemctl status annotation-backup.service
sudo journalctl -u annotation-backup.service -n 100 --no-pager
sha256sum --check /home/ck/annotation_db_backups/<timestamp>/SHA256SUMS
/home/cjg/annotation_tool/deploy/verify-postgres-backup.sh \
  /home/ck/annotation_db_backups/<timestamp>
```

备份必须有一份位于不同物理盘/异机/对象存储。只保留在 `/home/ck` 同一 NVMe 不是灾备。

## 测试与语法检查

```bash
uv run python -m py_compile \
  db.py annotation_repository.py preprocess_store.py server.py \
  preprocess.py classify.py export.py manage_state.py
uv run pytest -q
bash -n monitor.sh deploy/backup-postgres.sh deploy/verify-postgres-backup.sh
sudo nginx -t
```
