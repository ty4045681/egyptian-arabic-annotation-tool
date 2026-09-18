# 埃及方言音频标注平台

面向 6–20 名标注员、10 万条以上音频的 Web 标注平台。后端使用 Flask + PostgreSQL，支持原子任务领取、强制续领、版本化标注、并发安全自动保存、个人完成页和可审计纠正。

## 核心能力

| 功能 | 行为 |
|---|---|
| VAD + ASR 预处理 | Silero VAD 自动断句，DashScope Qwen3.5-Omni 阿拉伯语预转写 |
| PostgreSQL 状态 | 任务、segments、波形、assignment、session、历史和修订统一事务管理 |
| 原子领取 | `FOR UPDATE SKIP LOCKED` + 唯一约束；同一任务最多一人、同一用户最多一条 |
| 强制续领 | 刷新、Logout、会话超时、重新登录、会话接管和服务重启后仍恢复同一未完成任务 |
| 会话接管 | 失联设备可被自动替换；仍在线设备需确认后立即接管。旧会话不能再写入 |
| 串行自动保存 | 当前任务只允许一个在途保存；revision 防陈旧覆盖；operation ID 支持安全重试 |
| 原子完成/跳过 | 最后修改、状态、历史和 assignment 释放在一个数据库事务中提交 |
| 历史浏览 | Previous completed / Newer / Older 只读浏览，不释放当前任务 |
| 本人完成页 | 搜索、筛选、分页、只读详情；无未完成任务时可创建纠正草稿 |
| 版本化纠正 | 新草稿不影响当前 published 版本；提交后原子发布，放弃不改变原版本 |
| 直观时长 | 排行榜累计时长统一为 `xx h xx min` |
| 登录页速度图 | 最近 28 个自然日的新增有效标注音频时长（小时），外加四段七日平均虚线 |
| 音频代理 | Flask 鉴权，Nginx `X-Accel-Redirect`/sendfile 处理 Range |
| Admin 控制台 | 独立密钥/Session/CSRF、全局 Dashboard、质量信号、审计与任务管理 |
| 安全撤销/停用 | 不删除历史；原子撤销当前版本、重建 baseline draft、回收任务并阻止原提交者重领 |
| 无损迁移 | JSON manifest、SHA-256、可重入导入、round-trip 校验、兼容 JSON 回导 |
| 备份 | PostgreSQL custom dump + 校验 + 恢复演练 + 兼容 JSON 快照 |
| 场景元数据 | 版本化 `export-metadata` / `import-metadata`；训练旁文件；Excel 来源/核验列 |

## 架构

```text
Browser
  │ HTTPS
Cloudflare Tunnel
  │ 127.0.0.1:8080
Nginx
  ├─ API/Page → Gunicorn 127.0.0.1:8081 → Flask → PostgreSQL
  └─ Audio Range ← X-Accel-Redirect ← Flask session/path validation
```

PostgreSQL 是切换后的唯一在线真源。旧 `/home/ck/annotations` JSON 在迁移时冻结为只读归档，不参与在线写入；需要时由 `manage_state.py export-json` 从 PostgreSQL 重建兼容 JSON。

## 项目结构

```text
server.py                   Flask API / page / audio authorization
annotation_repository.py    所有业务事务和权限规则
db.py                       psycopg 连接池与 migration runner
migrations/                 PostgreSQL schema
manage_state.py             JSON 盘点、迁移、验证、回导、元数据导入导出、assignment 管理
preprocess.py               VAD + ASR，直接写 PostgreSQL
preprocess_store.py         预处理入库与人工标注保护
classify.py                 从 PostgreSQL 分类并更新 category
export.py                   从 PostgreSQL 导出 Excel
index.html                  标注操作台
completed.html              本人完成/跳过页面
login.html                  登录页、28 日速度图和排行榜
static/login-dashboard.js    登录页图表与排行榜（Chart.js）
static/vendor/               固定版本 Chart.js / annotation 插件（无 CDN）
admin.html/css/js           独立 Admin Dashboard 与管理交互
deploy/                     Nginx、备份、systemd、PostgreSQL 配置模板
tests/                      PostgreSQL/API/迁移/并发自动化测试
pyproject.toml + uv.lock     uv 环境与锁定依赖
```

## 环境

- Linux
- Python 3.12（由 uv 管理）
- PostgreSQL 16+
- Nginx
- uv 0.12+
- CPU 版 PyTorch（仅预处理 dependency group）

## Python 环境（必须使用 uv）

```bash
cd /home/cjg/annotation_tool
uv python install 3.12
uv sync --group dev

# 需要运行 VAD/ASR 预处理时再安装 CPU PyTorch 组
uv sync --group dev --group preprocess
```

所有 Python 命令都通过 `uv run` 执行，不使用系统 Python、pip 或 Conda：

```bash
uv run python manage_state.py schema
uv run pytest -q
uv run gunicorn -c gunicorn_config.py server:app
```

## 配置

复制 `config.example.json` 为 `config.json`。该文件只放音频路径、会话密钥和 VAD/ASR 参数：

```json
{
  "audio_dir": "/home/ck/ar_audios",
  "port": 8081,
  "audio_accel_prefix": "/_protected_audio",
  "secret_key": "随机 64 位十六进制字符串",
  "session_timeout_minutes": 30,
  "session_absolute_timeout_hours": 20,
  "session_presence_heartbeat_seconds": 30,
  "session_presence_lease_seconds": 150,
  "session_takeover_token_seconds": 60,
  "session_activity_throttle_seconds": 30,
  "offline_draft_retention_days": 7,
  "public_dashboard_timezone": "Asia/Shanghai",
  "vad": {},
  "asr": {}
}
```

生成 secret：

```bash
uv run python -c 'import secrets; print(secrets.token_hex(32))'
```

数据库连接只从环境变量读取，不能写入 `config.json`：

```bash
export ANNOTATION_DB_DSN='postgresql://annotation_app:...@127.0.0.1:5432/annotation_tool'
```

生产使用 `/etc/annotation-tool.env`（0600），模板见 `deploy/annotation-tool.env.example`。

### Admin 控制台

Admin 使用独立高熵密钥，不复用标注员用户名登录。生成密钥并将明文保存到团队
密码管理器，只把 SHA-256 摘要写入环境变量：

```bash
uv run python -c 'import hashlib,secrets; k=secrets.token_urlsafe(32); print(k); print(hashlib.sha256(k.encode()).hexdigest())'
```

```bash
export ANNOTATION_ADMIN_KEY_ID=primary
export ANNOTATION_ADMIN_KEY_SHA256='64 位小写十六进制摘要'
export ANNOTATION_ADMIN_COOKIE_SECURE=false  # 本地 HTTP；生产必须为 true
```

执行 migration 并启动服务后访问 `/admin`。控制台提供：

- 当前全量语料、pending/assigned 队列、音频和可训练时长快照。
- 按日期与时区查看历史 annotated/skipped/revoked 趋势。
- 每个标注员的当前贡献、标签比例、wall-clock 周转时间和记录明细。
- 单条/批量撤销预览与确认、任务详情/音频、质量规则信号和 Admin 审计；任何批量
  撤销均要求再次输入 Admin 密钥。
- 标注员停用同样要求二次密钥确认；停用会注销其 Session、回收在途任务，并撤销其仍为
  当前有效版本的成果。身份、版本与审计记录不会被物理删除。

撤销操作使用 expected-version、operation ID 和单数据库事务；被撤销任务从不可变
baseline 创建干净 draft 后重新入池，默认禁止原提交者再次领取。当前“速度”指标为
领取/重新打开到提交的 wall-clock 时间（含空闲），并非精确活跃操作时长。

## 初始化 PostgreSQL

先由管理员创建数据库和角色，再执行版本化 schema：

```bash
export ANNOTATION_DB_DSN='postgresql://annotation_owner:...@127.0.0.1:5432/annotation_tool'
uv run python manage_state.py apply-migrations
uv run python manage_state.py schema
```

Gunicorn 启动时只执行 schema 检查，不自动修改 schema。

## 旧 JSON 无损迁移

1. 停止旧服务，冻结 JSON。
2. 只读盘点并生成 manifest：

```bash
uv run python manage_state.py inspect-json \
  --annotations /home/ck/annotations \
  --audio /home/ck/ar_audios \
  --output /home/ck/migration/manifest.json
```

存在损坏 JSON、同 stem 不同扩展、`|/-` key 碰撞、缺音频或异常 assignment 时命令以非零状态退出，禁止迁移。

3. 导入并逐条校验：

```bash
uv run python manage_state.py migrate-json --manifest /home/ck/migration/manifest.json
uv run python manage_state.py verify-json \
  --manifest /home/ck/migration/manifest.json \
  --output /home/ck/migration/verification.json
```

4. 回导兼容 JSON（审计/回滚）：

```bash
uv run python manage_state.py export-json --output /home/ck/annotation_exports/$(date +%Y%m%d-%H%M%S)
```

导出永远写新目录，不覆盖迁移前快照。

## 预处理与分类

```bash
# VAD + 可选 ASR；自动跳过已预处理和所有人工/published 数据
uv run --group preprocess python preprocess.py -w 10

# 强制重新处理“没有人工修改”的 draft
uv run --group preprocess python preprocess.py --force

# 分类；仅更新 PostgreSQL category
uv run --group preprocess python classify.py --dry-run
uv run --group preprocess python classify.py
```

## 开发与测试

开发服务（需本地 PostgreSQL DSN）：

```bash
uv run python server.py --port 8081
```

完整测试（含 Playwright；本机已安装 Chromium，不要用 skip 掩盖失败）：

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q
```

默认使用测试夹具里的隔离 PostgreSQL。当前 pgserver 捆绑的是 **PostgreSQL 16.2**，不能把它跑两遍当成 16/18 矩阵。指定本机 PostgreSQL 18.6 二进制（仍是一次性隔离集群，不是 live 库）：

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q
```

GitHub Actions 用 PostgreSQL 16 与 18 **service** 矩阵，并通过 `ANNOTATION_TEST_PG_ADMIN_DSN` + 同主版本 `pg_dump`/`pg_restore` 创建一次性测试库。不要把生产 DSN 传给测试。`--target-dsn` / `ANNOTATION_DB_DSN` 不要带密码；libpq 从 `PGPASSWORD`、`~/.pgpass` 或 `PGSERVICEFILE` 读凭据。

100k 容量（一次性独立库，测完删除；Gunicorn 2 workers × 4 threads，与本机 4 核/~8GB 预览一致）：

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python \
  scripts/load_test_100k.py --artifact-dir docs/plans/load-test-100k
```

产物：`results.json`、`samples.jsonl`、`dataset.json`、`explain-*.json`。

测试通过范围包括：并发领取、同名登录竞态、会话接管与写入栅栏、revision/operation ID、Logout 后续领、完成/跳过、本人完成页权限、纠正草稿、Admin 鉴权/CSRF、批量撤销原子性、停用回收、并发管理操作、管理员来源筛选/统计、场景范围锁顺序、baseline 回填、JSON round-trip、Excel 和音频 Range、元数据导出/导入、dump/restore、Playwright 场景流程与会话接管。

## 登录页统计口径

公开 `GET /api/leaderboard` 仍返回原有 `leaderboard`，并增加 `annotation_speed`。该接口无需登录，只返回聚合结果，不含任务、事件或内部用户 ID。浏览器使用 `Cache-Control: no-cache, must-revalidate` 且登录页 `fetch(..., { cache: "no-store" })`，每 60 秒强制重新验证；源站用 60 秒进程内 TTL 合并查询，不依赖 Nginx `proxy_cache`。

速度图统计的是**当前仍然有效**的 `annotated` 任务音频时长（`annotation_tasks.duration`），按当前 published 版本的 `submitted_at` 在 `public_dashboard_timezone`（IANA 名称，默认 `Asia/Shanghai`）下的日历日分桶。`skipped`、已撤销、以及已被新版本取代的记录不计入；重新标注只计入新的当前版本及其提交日期。后端完成分桶，浏览器不得再用本地时区换算。没有标注的日期补零；今天标记为 `is_partial`。28 天按数组顺序分成四组，每组七日平均值是七天时长之和除以 7（零值日也参与分母）。非法时区会在进程启动时失败，不会静默回退。

图表由仓库内固定版本的 Chart.js 4.5.1 与 chartjs-plugin-annotation 3.1.0 绘制（见 `static/vendor/README.md` 的来源、MIT License 和 SHA-256）。运行时不访问 CDN。选择性窗口上的 `EXPLAIN` 必须使用 `007_annotation_speed_indexes.sql` 的两个部分索引；健康检查 schema 版本为 `[1, 2, 3, 4, 5, 6, 7, 8]`。

## 标注员会话

同一用户名同一时间只有一个可写入的网页会话。浏览器每 30 秒报告在线状态；150 秒没有心跳即可在新设备自动接管。仍在线的会话需要明确确认后才能立即接管，不必等待 30 分钟。30 分钟无真实操作会空闲退出；从登录或接管起最多 20 小时，不因心跳或操作延长。被动心跳、排行榜轮询不能续期空闲期限。

接管、退出或会话过期都不释放未完成任务。新设备恢复原 assignment、草稿、revision 和 lease token。原设备未得到服务器确认的编辑只保存在该浏览器的 IndexedDB 中，默认保留 7 天；新设备只能看到服务器已经确认的内容。

用户名登录不是身份认证。任何知道该用户名的人都可以尝试强制接管并查看该用户可见的数据。接管确认令牌只防止盲目重放和并发误踢，不能证明现实身份。

## 标注工作流

1. 输入固定用户名登录。若该名字仍在另一设备在线，需要确认后才能继续。
2. 有未完成任务时自动恢复；没有任务时点击 **Claim next task**。
3. 编辑文本、时间和 Bad quality。修改停止 3 秒后合并自动保存。
4. 点击 **Mark done**，或选择一个/多个 skip reason 后点击 **Skip task**。
5. 完成后停在 idle 页面，由标注员选择领取下一条或查看完成数据。
6. **Previous completed** 只读查看历史；Newer/Older 往返，不影响当前 assignment。
7. **Completed data** 仅显示本人记录；有未完成任务时不能开始纠正。

assignment 不按 72 小时自动过期。管理员只能通过明确、可审计命令回收：

```bash
uv run python manage_state.py assignments-list
uv run python manage_state.py assignment-release --username alice --reason 'confirmed by coordinator'
```

## 导出 Excel

```bash
uv run python export.py --output annotation_export.xlsx
```

只导出当前 published 的 annotated/skipped 版本。

## 生产部署与运维

完整步骤见 [DEPLOY.md](DEPLOY.md)，标注员操作见 [标注工具使用说明.md](标注工具使用说明.md)，故障排查见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
