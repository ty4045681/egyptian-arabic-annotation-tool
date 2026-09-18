# 断网恢复、会话接管与离线草稿：完整实施计划

日期：2026-09-17<br>
状态：待实现<br>
目标执行者：Grok 4.6<br>
适用范围：普通标注员会话；管理员会话保持现状

## 1. 最终决策

本项目继续保持“用户名即可登录、同一用户名只有一个有效网页会话”的产品规则，但不再让失联的旧页面阻塞新设备 30 分钟。

采用以下组合方案：

1. **在线租约（presence lease）**：浏览器定期报告在线状态，默认每 30 秒一次；150 秒没有心跳即视为旧设备失联。
2. **空闲会话（idle session）**：只有真实用户操作才延长，默认 30 分钟。被动心跳、排行榜轮询、后台刷新不能延长空闲期限。
3. **绝对会话上限（absolute lifetime）**：从登录或接管成功起最多 20 小时，不因任何操作延长。
4. **原子接管（atomic takeover）**：失联会话可以自动替换；仍在线的会话允许用户确认后立即接管，不要求等待空闲期限。
5. **写入栅栏（session fencing）**：接管完成后，旧会话不能再保存、提交、领取、放弃或重新打开任务；已经进入事务的旧写入与接管按数据库锁顺序线性化。
6. **assignment 与 web session 分离**：接管、退出或会话过期都不释放未完成任务；新设备继续恢复原 assignment、草稿、revision 和 lease token。
7. **本地离线草稿**：原设备使用 IndexedDB 保存尚未得到服务器确认的编辑和请求；关闭页面后在原设备可恢复。跨设备只能恢复服务器已经确认的数据。

默认参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `session_idle_timeout_minutes` | 30 | 无真实用户操作后的退出时间 |
| `session_absolute_timeout_hours` | 20 | 单次会话绝对上限 |
| `session_presence_heartbeat_seconds` | 30 | 新前端的在线心跳间隔 |
| `session_presence_lease_seconds` | 150 | 超过该时间没有心跳即可自动接管 |
| `session_takeover_token_seconds` | 60 | 强制接管确认凭证有效期 |
| `session_activity_throttle_seconds` | 30 | 服务端接受活动续期的最小间隔 |
| `offline_draft_retention_days` | 7 | 已失去服务器会话的本地草稿保留期 |

20 小时是本次明确的产品配置，不使用此前建议的 8 小时。

## 2. 交付目标与非目标

### 2.1 必须实现

1. 原浏览器关闭或断网后，用户可以在有网设备上立即开始登录流程，不再被固定阻塞 30 分钟。
2. 旧会话已经失联时自动接管；旧会话仍在线时只增加一次明确确认，不等待倒计时。
3. 接管是 PostgreSQL 单事务操作，并发接管只能有一个赢家。
4. 接管响应成功后，旧会话不可能再提交新的业务写入。
5. 新设备恢复同一用户的原 assignment，不自动领取新任务，也不释放旧任务。
6. 30 分钟空闲退出必须真实生效；打开页面但没有操作不能被后台心跳无限续期。
7. 绝对会话期限为 20 小时，并由服务端强制执行。
8. 前端区分 `session_replaced`、`idle_timeout`、`absolute_timeout` 和普通未登录，不再遇到所有 401 都立刻跳页。
9. 原设备的未确认编辑写入 IndexedDB；请求重试保持相同 `operation_id` 和完全相同的请求体。
10. 管理员“在线”状态改为在线租约口径，不再把 30 分钟内的失联用户都显示为在线。

### 2.2 明确不做

1. 不允许同一用户名有多个可同时写入的活动会话。
2. 不依赖 WebSocket；普通 HTTPS 心跳足够。
3. 不把 `beforeunload`、`pagehide`、`sendBeacon` 或 `navigator.onLine` 当作正确性依据。
4. 不在接管时释放、转移或重建 assignment。
5. 不自动合并两个设备上各自尚未上传的文本。
6. 不修改管理员登录、管理员 CSRF、管理员 idle/absolute session 设计。
7. 本轮不引入密码、短信、邮件或 SSO；但必须在界面和文档中承认用户名登录无法防止恶意冒用。

## 3. 当前实现与问题定位

### 3.1 当前服务端

- `migrations/001_initial.sql` 的 `active_sessions.user_id` 是主键，因此每个标注员最多一行活动会话。
- `annotation_repository.login()` 对该行 `FOR UPDATE`，只要 `expires_at > now()` 就抛出通用 `ConflictError`。
- `server.api_login()` 每次生成新 UUID SID，但没有正常恢复、失联识别或接管分支。
- `validate_session()` 只返回有效用户或 `None`，无法告诉前端是超时、被替换、被停用还是主动退出。
- `logout()` 已经按 `username + session_id` 删除，因此旧页面退出不会误删新页面的会话；这一性质必须保留。

### 3.2 当前前端

- `index.html` 每 120 秒无条件请求 `GET /api/heartbeat`。
- 该心跳同时把 `active_sessions.expires_at` 推到 30 分钟以后，所以打开页面即使完全没有操作，也会无限续期。
- 所有 401 都立即执行 `location.href = "/login.html"`，可能在本地尚有未保存文字时直接销毁页面内存。
- 自动保存失败后的 `pendingSave`、`operation_id` 和请求体仅保存在内存；离线关闭页面后不可恢复。
- `visibilitychange` 和 `beforeunload` 只能尽力保存，不能保证断网或进程被杀时执行成功。

### 3.3 可复用的现有能力

- assignment 本来就独立于 web session；现有测试已经验证 logout 后再次登录会恢复同一任务。
- 写入已经携带 assignment `lease_token`、`expected_revision` 和 `operation_id`。
- `save_draft`、`complete`、`abandon`、`reopen` 已有数据库事务和幂等操作表。
- revision 乐观锁可以继续处理业务版本冲突，但不能代替会话接管栅栏。

## 4. 目标状态机与不变量

### 4.1 会话状态

服务端需要能区分以下状态：

| 状态代码 | 条件 | HTTP/前端行为 |
|---|---|---|
| `valid` | SID、generation 匹配，idle 和 absolute 均未到期 | 正常处理 |
| `session_replaced` | 用户存在活动行，但 SID 或 generation 已变化 | 401；旧页面冻结写入 |
| `idle_timeout` | 当前 SID 匹配，但 `expires_at <= now()` | 401；要求重新登录 |
| `absolute_timeout` | 当前 SID 匹配，但 `absolute_expires_at <= now()` | 401；要求重新登录 |
| `logged_out` | 没有活动行 | 401；正常登录页 |
| `account_deactivated` | annotator 非 active | 403；不能通过接管恢复 |

判断顺序必须稳定。若 SID 已被替换，返回 `session_replaced`；若仍是当前 SID，再判断 absolute 和 idle。

### 4.2 核心不变量

1. 每个 `user_id` 最多一条 `active_sessions` 行。
2. 每次创建新会话或接管都生成新 `session_id`；替换现有行时使 `generation = generation + 1`，没有旧行时从 1 开始。
3. 普通恢复同一个有效 SID 不增加 generation。
4. 接管 token 同时绑定用户名、它观察到的 generation 和当前 SID 的 SHA-256 指纹；任一项变化后 token 立即失效。指纹只放在签名 token 内，不作为独立 API 字段返回。
5. 所有普通标注员业务写事务必须验证 `user_id + session_id + generation`。
6. 写事务对 session 行持有共享锁，接管对同一行持有更新锁：
   - 旧写入先取得锁：允许它先完成，接管等待；
   - 接管先完成：旧写入验证失败；
   - 接管响应返回后，不会再有旧会话写入提交。
7. session 失效不删除 assignment。
8. 心跳只能延长 idle 到“当前服务端时间 + 30 分钟”，绝不能延长 absolute deadline。
9. 所有时间判定使用 PostgreSQL `now()`，避免应用进程时钟差异。

## 5. 数据库迁移 `006_session_takeover.sql`

迁移必须是 additive，保留旧 `expires_at` 列，以便同 schema 回退旧代码。该列从此明确表示 idle deadline。

建议 SQL 结构：

```sql
ALTER TABLE active_sessions
    ADD COLUMN generation BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN last_activity_at TIMESTAMPTZ,
    ADD COLUMN absolute_expires_at TIMESTAMPTZ;

-- 已在线用户在部署时获得一次平滑过渡，不因历史 login_time 很早而集体退出。
UPDATE active_sessions
SET last_activity_at = LEAST(last_seen_at, expires_at),
    absolute_expires_at = now() + interval '20 hours'
WHERE last_activity_at IS NULL OR absolute_expires_at IS NULL;

-- 先压平历史异常值，再进入新代码语义。
UPDATE active_sessions
SET expires_at = LEAST(expires_at, absolute_expires_at);

ALTER TABLE active_sessions
    ALTER COLUMN last_activity_at SET NOT NULL,
    ALTER COLUMN absolute_expires_at SET NOT NULL,
    ALTER COLUMN last_activity_at SET DEFAULT now(),
    ALTER COLUMN absolute_expires_at SET DEFAULT (now() + interval '20 hours'),
    ADD CONSTRAINT active_sessions_generation_positive CHECK (generation > 0);

COMMENT ON COLUMN active_sessions.last_seen_at IS
    'Presence heartbeat; does not by itself extend idle expiry';
COMMENT ON COLUMN active_sessions.expires_at IS
    'Server-enforced idle deadline';
COMMENT ON COLUMN active_sessions.absolute_expires_at IS
    'Non-sliding absolute session deadline';
```

不要增加 `expires_at <= absolute_expires_at` 的数据库 CHECK。新代码必须通过 `LEAST(...)` 和测试维持该不变量，但旧版代码不知道 absolute 字段；若发生同 schema 回退，硬约束会让旧心跳在 20 小时后写入失败。

`last_activity_at` 和 `absolute_expires_at` 的数据库 DEFAULT 必须保留。它们不仅服务新代码，也确保回退后的旧版 INSERT（不会显式提供这两列）仍能创建会话。回退演练必须实际用旧版形状执行一次 INSERT，不能只验证 SELECT。

同时新增审计表：

```sql
CREATE TABLE annotator_session_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES annotators(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    generation BIGINT NOT NULL,
    previous_generation BIGINT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_annotator_session_events_user_time
    ON annotator_session_events(user_id, occurred_at DESC);
```

允许的 `event_type` 至少包括：`login`、`resume`、`stale_takeover`、`forced_takeover`、`logout`。不要保存原始 SID、完整 IP 或完整 User-Agent；如确实需要关联，仅保存不可逆摘要和粗粒度设备类别。

迁移测试需更新 `tests/test_metadata_migrations.py` 中写死的版本列表，健康检查期望变为 `[1, 2, 3, 4, 5, 6]`。

## 6. 配置与 Cookie 策略

### 6.1 `config.example.json`

增加：

```json
{
  "session_timeout_minutes": 30,
  "session_absolute_timeout_hours": 20,
  "session_presence_heartbeat_seconds": 30,
  "session_presence_lease_seconds": 150,
  "session_takeover_token_seconds": 60,
  "session_activity_throttle_seconds": 30,
  "offline_draft_retention_days": 7
}
```

校验范围：

- idle：5～1440 分钟；默认 30。
- absolute：必须大于等于 idle，最大可限制为 24 小时；默认 20 小时。
- heartbeat：15～120 秒；默认 30。
- presence lease：至少为 heartbeat 的 3 倍；默认 150。
- takeover token：30～300 秒；默认 60。

### 6.2 Flask session cookie

1. `PERMANENT_SESSION_LIFETIME` 改为 20 小时，数据库另外执行 30 分钟 idle 判断。
2. 建议设置 `SESSION_REFRESH_EACH_REQUEST = False`，使 Cookie 本身也是非滑动的绝对期限。
3. 保留 `HttpOnly` 和 `SameSite=Lax`。
4. 生产 HTTPS 设置 `Secure=True`；测试环境仍可为 false。
5. Flask Cookie 只承载 `user`、`sid`、`generation`。所有有效性仍以数据库为准。

不要把 absolute deadline 只放在 Cookie 中；用户可以持有旧 Cookie，服务端数据库必须独立拒绝。

## 7. 后端会话领域模型

建议在 `annotation_repository.py` 增加明确类型：

```python
@dataclass(frozen=True)
class SessionFence:
    user_id: uuid.UUID
    username: str
    session_id: uuid.UUID
    generation: int

@dataclass(frozen=True)
class SessionState:
    status: str
    fence: SessionFence | None
    idle_expires_at: datetime | None
    absolute_expires_at: datetime | None
    last_seen_at: datetime | None
```

新增专用异常，不再用所有业务共享的通用 `ConflictError`：

- `ActiveSessionConflict`：携带 username、observed generation、last_seen、login_time。
- `SessionChangedConflict`：接管 token 所绑定的 generation 已变化。
- `SessionFenceError`：旧会话试图执行业务写入。

对外错误不得返回 SID、数据库 UUID、IP 或 User-Agent。

### 7.1 `inspect_session(username, sid, generation)`

只读查询并返回上表状态，不刷新任何时间。`current_user()` 和 `login_required` 使用该函数。

有效用户返回值必须包含 `session_id` 和 `generation`，供后续事务构造 `SessionFence`。不要只返回 user ID。

兼容迁移前已经打开的页面：如果 Flask Cookie 有匹配的 username/SID 但还没有 `generation`，可以从数据库读取当前 generation、将本次请求视为合法 legacy session，并立即把 generation 写回新 Cookie。只允许这一次“SID 匹配时补 generation”；SID 不匹配时绝不能借此恢复旧会话。

### 7.2 原子普通登录 `login_or_resume`

伪代码：

```text
BEGIN
  ensure/create annotator
  SELECT annotator FOR UPDATE
  SELECT active_session FOR UPDATE

  if account deactivated:
      403

  if row exists and request cookie SID/generation exactly matches
     and idle/absolute valid:
      update last_seen_at only
      return resumed, generation unchanged

  if row missing or idle expired or absolute expired:
      replace row with new SID
      generation = coalesce(old generation, 0) + 1
      login_time = last_seen = last_activity = now
      expires_at = now + 30 minutes
      absolute_expires_at = now + 20 hours
      return login

  if last_seen_at <= now - 150 seconds:
      replace row exactly as above
      return stale_takeover

  otherwise:
      raise ActiveSessionConflict(observed generation, safe timestamps)
COMMIT
```

`expires_at` 必须取 `LEAST(now() + idle_ttl, absolute_expires_at)`。

同一用户名并发首次登录依靠 annotator 行锁串行；第一个成功后，第二个看到新鲜 presence 并收到 `session_active`，不能也自动成功。

### 7.3 强制接管 `force_takeover`

初次冲突后由 API 生成短期签名 token，建议使用 Flask 已有 secret 和独立 salt，通过 `itsdangerous.URLSafeTimedSerializer` 保存：

```json
{
  "username": "alice",
  "observed_generation": 7,
  "observed_session_fingerprint": "sha256-of-current-random-sid",
  "purpose": "annotator-session-takeover"
}
```

接管事务：

```text
BEGIN
  lock annotator FOR UPDATE
  lock active_session FOR UPDATE
  reject deactivated user
  if current generation != observed_generation
     or sha256(current SID) != observed_session_fingerprint:
      return 409 session_changed; do not blindly kick the newer session
  replace SID
  generation = generation + 1
  reset login/seen/activity/idle/absolute deadlines
  record forced_takeover
COMMIT
```

token 的作用是证明这是用户刚刚确认过的同一次冲突并防止并发误踢，不是身份认证。用户名登录本身仍无法证明现实身份。必须绑定 SID 指纹，因为 logout 删除旧行后，新行的 generation 可能再次从 1 开始；只绑定 generation 会让一分钟内的旧 token 有机会命中新会话。

### 7.4 Presence 与 activity

用 `POST /api/session/heartbeat` 取代会修改状态的 GET。请求：

```json
{"activity": true}
```

处理规则：

1. 先按当前 SID/generation/期限验证，已过期会话不可通过心跳复活。
2. 总是更新 `last_seen_at = now()`。
3. 仅当 `activity=true` 时更新 `last_activity_at` 和 idle deadline。
4. `activity=true` 的更新按配置节流；但 last_seen 仍可更新。
5. 仅真实 activity 更新 assignment 的 `last_activity_at`；被动 presence 不更新。
6. 返回服务端时间和两个期限，供前端倒计时：

```json
{
  "ok": true,
  "server_time": "...",
  "idle_expires_at": "...",
  "absolute_expires_at": "..."
}
```

业务写操作成功时也应在同一事务中刷新真实 activity；读取排行榜、dashboard、waveform 或后台轮询不能刷新。

保留旧 `GET /api/heartbeat` 一个兼容发布周期：它只更新 presence，明确不延长 idle，并返回 `Deprecation`/日志标记。新前端只使用 POST。旧页面发生真实保存、提交等业务写入时仍会由业务事务刷新 activity；不能为了兼容而恢复“被动心跳无限续期”。下一发布周期删除 GET 兼容入口。

`GET /api/current-user` 同时作为安全的会话 bootstrap，返回用户名、服务端期限和非敏感客户端参数：heartbeat interval、idle warning threshold、offline retention。它不返回 SID、generation 或 takeover 配置内部值；`index.html` 与 `completed.html` 不要各自硬编码另一套间隔。

## 8. 写入栅栏与事务锁顺序

这是本方案的关键部分，不能只在 Flask 装饰器检查一次。

### 8.1 栅栏函数

```python
def _require_session_fence(cur, fence: SessionFence) -> None:
    row = cur.execute(
        """SELECT session_id, generation,
                  expires_at > now() AS idle_valid,
                  absolute_expires_at > now() AS absolute_valid
             FROM active_sessions
            WHERE user_id = %s
            FOR SHARE""",
        (fence.user_id,),
    ).fetchone()
    if not row:
        raise SessionFenceError(code="not_authenticated")
    if str(row[0]) != str(fence.session_id) or row[1] != fence.generation:
        raise SessionFenceError(code="session_replaced")
    if not row[3]:
        raise SessionFenceError(code="absolute_timeout")
    if not row[2]:
        raise SessionFenceError(code="idle_timeout")
```

`FOR SHARE` 使 takeover 的 `UPDATE/SELECT FOR UPDATE` 等待已经验证通过的旧写入完成。接管 API 只有在这些旧事务完成后才返回，因此提供“接管成功后旧会话再也不能提交”的清晰边界。

必须先按 user 锁住并读取 session 行，再在同一事务里区分失败原因。不能把 SID、generation 和期限全部放到 `WHERE` 后只得到一个无法解释的空结果。

### 8.2 固定锁顺序

所有标注员写事务统一：

1. `annotators` 行 `FOR UPDATE`；
2. `active_sessions` 行 `FOR SHARE`（接管使用 `FOR UPDATE`）；
3. assignment；
4. task；
5. annotation version；
6. segments/reviews/events。

不能在部分路径先锁 assignment、另一部分先锁 session，否则会引入死锁。

### 8.3 必须覆盖的普通用户写入口

| HTTP 路由 | Repository 方法 | 要求 |
|---|---|---|
| `POST /api/assignment/claim` | `claim` | fence 后才能恢复或领取 |
| `POST /api/assignment/current/abandon` | `abandon` | fence 必须早于 operation replay |
| `PATCH/POST /api/assignment/current` | `save_draft` | fence 必须早于 operation replay 和 revision 检查 |
| `POST /api/assignment/current/complete` | `complete` | 同上 |
| `POST /api/completed/<id>/reopen` | `reopen_completed` | fence 后才能创建 revision assignment |

管理员写入口继续使用管理员 session 体系，不套普通用户 fence。

推荐让上述五个 repository 方法显式接收必填的 `SessionFence`，而不是可选参数。更新直接调用这些方法的测试和 `scripts/smoke_repo.py`，防止未来新增 HTTP 路由时忘记传 fence。可以增加统一测试辅助函数 `login_actor(name)`，返回 user、SID、generation 和 fence，减少机械代码。

只读接口继续在 `login_required` 验证即可；接管瞬间已经开始的只读响应允许完成，不影响数据完整性。

## 9. HTTP API 合同

### 9.1 `POST /api/login`

请求保持兼容：

```json
{"username": "Ahmed Ali"}
```

成功：

```json
{
  "success": true,
  "username": "Ahmed Ali",
  "session": {
    "mode": "login",
    "idle_expires_at": "...",
    "absolute_expires_at": "..."
  }
}
```

`mode` 为 `login`、`resume` 或 `stale_takeover`。不要向 JS 返回 SID。

仍在线冲突：HTTP 409。

```json
{
  "success": false,
  "code": "session_active",
  "error": "This name is currently active on another device.",
  "active_session": {
    "last_seen_at": "...",
    "login_time": "..."
  },
  "takeover_token": "signed-short-lived-token",
  "takeover_token_expires_in": 60
}
```

### 9.2 `POST /api/login/takeover`

请求：

```json
{
  "username": "Ahmed Ali",
  "takeover_token": "..."
}
```

成功响应与 login 相同，`mode=forced_takeover`。token 过期返回 400 `takeover_token_expired`；generation 已变化返回 409 `session_changed`，登录页重新执行普通 login 获取最新状态。

### 9.3 认证失败响应

`login_required` 统一返回：

```json
{
  "error": "Session was replaced by another login.",
  "code": "session_replaced",
  "redirect": "/login.html?reason=session_replaced"
}
```

其他 code：`idle_timeout`、`absolute_timeout`、`not_authenticated`。停用账户使用 403 `account_deactivated`。

HTML 页面路由可继续重定向，但应保留 `reason` 查询参数。API 不返回 HTML。

### 9.4 Logout

保持 POST，并继续只删除完全匹配的 `username + SID + generation`。被替换的旧页面调用 logout 不得删除新会话。成功后清空当前 Flask cookie。

### 9.5 `GET /api/current-user`

成功响应扩展为：

```json
{
  "user": "Ahmed Ali",
  "session": {
    "server_time": "...",
    "idle_expires_at": "...",
    "absolute_expires_at": "...",
    "heartbeat_seconds": 30,
    "idle_warning_seconds": 120,
    "offline_draft_retention_days": 7
  }
}
```

它是 `index.html` 和 `completed.html` 的单一客户端配置来源。不要返回 SID、generation、presence lease 判定阈值或签名 token。

## 10. 登录页交互

修改 `login.html`，状态机而非单一错误字符串：

1. `idle`：输入用户名。
2. `submitting`：禁用按钮，防止重复请求。
3. `active_conflict`：显示：
   - “此用户名刚刚仍在另一设备使用”；
   - 最近在线时间；
   - 主按钮“继续登录并退出旧设备”；
   - 次按钮“返回修改用户名”。
4. `taking_over`：提交 takeover token。
5. `session_changed`：说明状态已经变化，自动重新检查一次，不循环自动接管。
6. `success`：使用 `location.replace("/")`，避免后退回到旧登录提交页。

文案不要声称用户名已验证。若用户点击接管，必须明确旧页面随后无法保存。

若访问 `/login.html` 时 Cookie 对应会话仍有效，可直接重定向 `/`；如需切换用户名，用户先明确退出。

## 11. 工作区会话与网络体验

### 11.1 活动检测

监听可信的用户事件：`pointerdown`、`keydown`、文本 `input`、音频 `play/seeked`、保存/提交/领取按钮。忽略纯 `mousemove`、自动播放时间更新和程序合成事件。只记录“自上次心跳后发生过活动”的布尔位，不向服务端发送客户端时间作为依据。

每 30 秒 POST heartbeat：

- 页面可见且发生过活动：`activity=true`；
- 无活动：`activity=false`；
- 页面隐藏：允许浏览器节流；不要求保持精确在线；
- 请求失败：显示离线状态但不自动登出，保留 activity 位等待下一次成功。

`online` 事件可以触发一次立即重试，但不能仅凭 `navigator.onLine=true` 判断服务器可达。

离线期间的本地点击和输入不能延长服务器 idle deadline。若断网超过剩余空闲时间，恢复网络后先要求重新登录；登录成功仍恢复同一 assignment，再按 IndexedDB outbox 重试。这是服务端超时约束，不应通过伪造客户端时间绕过。

### 11.2 401 处理

重写 `api()`：

- `session_replaced`：禁止后续网络写入，先保证当前编辑已落到 IndexedDB，再显示不可关闭的接管提示；不能直接跳登录页。
- `idle_timeout` / `absolute_timeout`：保存本地草稿后显示“会话已过期，重新登录继续”；点击后跳转。
- `not_authenticated`：没有本地脏数据时可直接跳转；有脏数据仍先保存并提示。
- 普通 409 revision conflict 保持现有冲突逻辑，不与 session replacement 混为一谈。

### 11.3 原页面恢复网络

旧页面在被接管后恢复：

1. heartbeat 或首次 API 请求得到 `session_replaced`；
2. 停止 autosave、complete、claim、abandon 重试；
3. 音频和当前文字可以留在只读页面；
4. 提供“复制/导出本地未保存内容”和“重新登录”操作；
5. 不自动反向接管新设备，避免两个设备互相踢出。

## 12. IndexedDB 离线草稿与可靠重试

### 12.1 为什么必须单独实现

会话接管只能恢复服务器已经保存的草稿。旧设备断网后新输入的文字如果只在 JavaScript 内存中，关闭页面即丢失。`beforeunload` 无法解决断网和浏览器进程被杀场景。

### 12.2 数据库结构

数据库名：`annotation-offline-v1`，至少两个 object store：

1. `working_drafts`，key 为 `username + ":" + task_id`：

```json
{
  "schema_version": 1,
  "username": "alice",
  "task_id": "...",
  "version_id": "...",
  "lease_token": "...",
  "server_revision": 4,
  "segments": [],
  "scene_review": {},
  "dirty_segment_ids": [1, 3],
  "updated_at": "...",
  "status": "dirty"
}
```

2. `outbox`，key 为 `operation_id`，保存发送时不可变的完整请求：

```json
{
  "operation_id": "...",
  "username": "alice",
  "task_id": "...",
  "route": "/api/assignment/current",
  "method": "PATCH",
  "body": {},
  "created_at": "...",
  "attempts": 0
}
```

outbox 至少覆盖 draft save、complete 和 abandon；这些操作都有 operation ID。claim 没有本地编辑载荷且服务端会返回现有 assignment，不必进入 outbox。reopen 可在以后统一，但不是本轮离线恢复的阻塞项。

### 12.3 写入顺序

1. 每次编辑先更新内存，然后用短 debounce 写 `working_drafts`；不能等页面关闭才写。
2. 准备网络保存时，先生成 `operation_id` 和冻结请求体。
3. 在 IndexedDB 成功写入 outbox 后，才发 HTTP。
4. 网络失败、超时或页面关闭时保留同一个 outbox 项。
5. 服务器确认成功后，在一个本地事务中删除 outbox 项、更新 server revision，并清理已确认的 dirty 标记。
6. 如果服务器已提交但响应丢失，重放相同 operation ID 和相同 body，利用现有 operation 表返回原结果。
7. 任何重试都不能生成新的 operation ID；否则失去幂等保证。

### 12.4 页面重新打开

1. 先获取当前会话，再读取该用户名的 outbox。
2. 对 complete/abandon 等终态请求，允许先用原 operation ID 和原请求体重放一次：服务器可能已经提交、只是响应丢失；repository 必须在 assignment 检查前处理同请求的 operation replay。
3. 获取服务器 assignment，再读取同用户名、同 task 的 working draft。
4. assignment、lease token、version 均匹配时：
   - 有 outbox：按创建顺序重放；
   - 只有本地 dirty draft：比较 server revision，匹配则提示恢复并保存；
   - revision 不匹配：进入人工冲突界面，不自动覆盖。
5. task 不匹配或 assignment 已结束：保留为“孤立本地草稿”，允许导出或丢弃，不发送到别的任务。
6. complete/abandon 得到服务器确认后清理对应 working draft 和 outbox。

Background Sync 可以以后作为增强，但不能作为首版正确性依赖，因为浏览器支持并不完整。首版依靠 IndexedDB、页面打开时重试和 `online` 事件即可。

### 12.5 数据边界

- 同设备、同浏览器配置：可以恢复本地未上传内容。
- 新设备：只能看到服务器最后确认的内容。
- 清除站点数据、无痕窗口结束、浏览器主动清理存储后，本地草稿可能消失。
- 本地数据默认 7 天清理；有未确认内容时 logout 必须提示用户，不能静默删除。

## 13. 管理端与统计口径

更新 `active_session_exists()`、`admin_annotators()` 和 `admin_annotator_detail()`：

```text
online =
  expires_at > now
  AND absolute_expires_at > now
  AND last_seen_at > now - presence_lease
```

显示字段：

- `last_seen_at`：最近在线心跳；
- `last_activity_at`：最近真实操作；
- `online`：短租约在线；
- assignment 的 `last_activity_at`：只由真实活动或业务写入更新。

不要把会话 generation、takeover token 或 SID 暴露到管理员 API。

建议增加结构化日志/指标：

- `annotator_login_total{mode=login|resume|stale_takeover|forced_takeover}`
- `annotator_session_rejected_total{reason=...}`
- `annotator_takeover_conflict_total`
- `offline_outbox_replay_success/failure`（客户端日志可聚合）
- heartbeat 写入频率和数据库耗时

## 14. 安全边界

当前产品使用用户名作为唯一登录输入，因此：

1. 任何知道用户名的人都可以尝试强制接管并查看该用户可见的数据。
2. takeover token 只能防止盲目重放、并发误踢和跨步骤竞态，不能证明用户真实身份。
3. “一个在线会话”是并发控制，不是认证强度。
4. 若数据敏感，后续至少加入个人 PIN、一次性验证码或 SSO；加入真实认证后，forced takeover 必须在重新认证后进行。

同时完成基础 Cookie 加固：生产 HTTPS 使用 Secure、HttpOnly、SameSite；登录和会话 API 设置 `Cache-Control: no-store`。

## 15. 测试计划

### 15.1 Repository 单元/集成测试

新增 `tests/test_annotator_sessions.py`，覆盖：

1. 首次登录创建 generation 1、30 分钟 idle、20 小时 absolute。
2. 同 SID 有效恢复不增加 generation。
3. 不同 SID 在 presence 新鲜时返回 `ActiveSessionConflict`。
4. presence 过期后自动接管，generation 加一。
5. idle 过期和 absolute 过期都允许新登录，但返回/记录不同原因。
6. forced takeover 只接受观察到的 generation。
7. 同一个 takeover token 重放失败。
8. 冲突 token 签发后执行 logout + 新 login，即使 generation 数值重新为 1，旧 token 也因 SID 指纹不同而失败。
9. 两个并发 stale takeover/forced takeover 只有一个成功。
10. 旧 SID logout 不删除新 SID。
11. presence heartbeat 不延长 idle；activity heartbeat 延长 idle但不超过 absolute。
12. 20 小时到期后任何 heartbeat 都不能复活。
13. deactivated annotator 不能登录或接管。
14. assignment 在 logout、idle timeout、stale takeover、forced takeover 后均保持。

### 15.2 栅栏并发测试

使用 `threading.Barrier` 和两个数据库连接，明确验证两种线性化顺序：

1. 旧 save 先取得 session 共享锁，takeover 等待；save 提交后 takeover 成功。
2. takeover 先更新 generation，旧 save 的 fence 检查失败且 segments/revision/operation 表均未变化。
3. takeover API 返回后，旧 save/complete/claim/abandon/reopen 全部失败。
4. fence 失败必须发生在 operation replay 之前，避免旧会话通过已知 operation ID探测响应。
5. 锁顺序测试无死锁，现有 scope/deactivation 并发回归继续通过。

### 15.3 API 测试

扩展 `tests/test_api.py`：

- 200 login/resume/stale takeover 契约。
- 409 `session_active` 含安全字段和短期 token。
- takeover 成功，两个 test client 中旧 client 得到 `session_replaced`。
- token 过期、篡改、用户名不匹配、generation 变化。
- idle/absolute 401 code 正确。
- 新 client 恢复同一 task、lease token 和最新 revision。
- GET heartbeat 兼容行为与 POST heartbeat 新行为。
- 页面路由重定向保留 reason。

### 15.4 Browser/Playwright 测试

新增 `tests/browser/test_session_takeover.py`：

1. Context A 登录并领取任务；Context B 同用户名登录看到接管确认。
2. B 接管后进入相同任务；A 下次请求出现替换提示且不能写。
3. A 模拟 offline 并关闭；等待 presence lease 后 B 普通登录自动成功。
4. A offline 编辑后关闭并重新打开同 context，本地草稿被发现并恢复。
5. B 新 context 不应看到 A 从未上传的本地文字，只看到服务器草稿。
6. 页面持续打开并只运行 heartbeat，时间推进 30 分钟后会话仍然 idle timeout。
7. 有真实 activity 时 idle 滑动，但 20 小时 absolute 不滑动。
8. 401 不会在写 IndexedDB 之前导航离开。

测试时间必须通过可注入 clock、配置为数秒的 TTL 或直接更新数据库时间字段完成，不能真实 sleep 30 分钟或 20 小时。

### 15.5 现有回归

至少运行：

```text
uv run pytest -q
uv run pytest -q tests/browser/test_session_takeover.py
uv run pytest -q tests/browser/test_scene_workflow.py
```

并确认：

- 原有 same-name atomic login 测试改成新状态机预期。
- assignment logout/resume、revision conflict、operation replay 全部仍通过。
- 管理员 session、管理员 CSRF 和管理员写操作没有行为变化。
- 100k claim/load harness 的登录和领取契约仍可运行。

## 16. 文件级改动清单

| 文件 | 计划改动 |
|---|---|
| `migrations/006_session_takeover.sql` | generation、activity、absolute、session events |
| `annotation_repository.py` | SessionState/Fence、登录状态机、接管、心跳、事务内 fence、online 查询 |
| `server.py` | 新登录/接管 API、结构化 401、POST heartbeat、20 小时配置、Cookie 策略 |
| `login.html` | session_active 确认与 takeover 状态机 |
| `index.html` | 活动感知心跳、401 分流、旧会话冻结、IndexedDB working draft/outbox |
| `completed.html` | 结构化 401 与 heartbeat；无编辑 outbox |
| `config.example.json` | 新会话参数及 20 小时默认值 |
| `README.md` | 会话语义、用户名认证边界 |
| `DEPLOY.md` | 006 迁移、部署/验证/回退 |
| `tests/conftest.py` | 新默认配置、session actor/fence fixture |
| `tests/test_annotator_sessions.py` | 状态机、并发、fence 测试 |
| `tests/test_api.py` | 新 API 合同与旧客户端兼容 |
| `tests/browser/test_session_takeover.py` | 双设备、断网、IndexedDB 验收 |
| `tests/test_metadata_migrations.py` | schema 版本 6 和列/约束验证 |
| `scripts/smoke_repo.py` | 显式 SessionFence 调用 |

如 `index.html` 的会话/IndexedDB 代码明显膨胀，应抽到 `static/annotator-session.js` 和 `static/offline-drafts.js`，并为静态路由增加白名单；不要继续把复杂状态机压成一行脚本。

## 17. 分阶段实施顺序

### P0：锁定契约

- 先把本计划中的错误 code、默认 TTL、API JSON 和 session 状态写成测试。
- 不先改 UI 猜测后端返回。

### P1：数据库与纯 Repository 状态机

- 完成 006、配置读取、SessionState/Fence、login/resume/stale/forced takeover。
- 完成 repository 并发测试。

### P2：事务内写入栅栏

- 改五个普通用户写入口及其所有调用方。
- 先保证并发测试，再接 UI。

### P3：API 与登录页面

- 完成结构化错误、短期签名 token、登录确认页面。
- 两个 Flask test client 完成接管验收。

### P4：工作区 heartbeat/idle 行为

- 拆 presence 与 activity。
- 删除所有 401 立即跳转行为。
- 更新 admin online 口径。

### P5：IndexedDB 草稿/outbox

- 先实现 working draft，再实现 immutable outbox 和相同 operation ID 重放。
- 完成断网关闭/重开浏览器测试。

### P6：兼容、文档与部署演练

- 旧 GET heartbeat 兼容窗口。
- 全量 pytest、Playwright、迁移 dry run、同 schema 回退演练。
- 更新 README/DEPLOY/config example。

每个阶段完成后保持测试绿色，不要把数据库、API、前端和离线存储全部堆到一次不可审查的提交中。

## 18. 部署、灰度和回退

### 18.1 部署

1. 备份 PostgreSQL，记录当前代码 commit。
2. 在维护窗口执行 `uv run python manage_state.py apply-migrations`。
3. 部署后端和新 HTML/JS，重启 Gunicorn。
4. `/api/health` 必须返回 `[1,2,3,4,5,6]`。
5. 用两个真实浏览器/无痕窗口完成 login → conflict → takeover → old-session-rejected 冒烟。
6. 再测试 offline → close → login/resume 和 assignment 不变。
7. 前 24 小时观察 takeover 量、session rejection、保存冲突和数据库心跳写入量。

页面脚本内嵌于 HTML，已有打开页面不会自动换成新逻辑。部署时保留旧 GET heartbeat 兼容，并在 UI/公告中要求正在标注的用户刷新一次。登录页、工作区和会话 API 增加 `Cache-Control: no-store`。

### 18.2 回退

006 只增加列/表并保留旧 `expires_at`，因此代码可同 schema 回退，不回滚数据库迁移：

1. 停止新代码写入并部署上一版本。
2. 上一版本忽略新列，恢复原 30 分钟冲突行为。
3. 不删除 generation、absolute 或审计数据。
4. 记录回退期间新旧会话语义差异；再次前滚时新代码会重新设置期限。

回退会重新引入“失联后等待 30 分钟”的旧问题，只作为故障恢复，不是长期运行模式。

## 19. 完成定义

只有以下条件全部满足才算完成：

1. 用户不再因为失联旧页面被强制等待 30 分钟。
2. live session 有明确确认，stale session 自动接管。
3. 接管完成后旧会话的五类业务写入全部被数据库事务内 fence 拒绝。
4. 新设备恢复同一 assignment 和服务器最新草稿。
5. 空闲 30 分钟与绝对 20 小时均有自动化测试证明，presence 不会绕过它们。
6. 原设备离线关闭后，本地未确认编辑可以从 IndexedDB 恢复或导出。
7. 跨设备不承诺恢复从未上传的数据，界面文案没有误导。
8. 并发接管、请求响应丢失重放、旧 logout、不同行为 401 均有覆盖。
9. 全量后端与浏览器测试通过，管理员认证无回归。
10. README、DEPLOY、config example、健康检查版本全部更新。

## 20. 实现时禁止的捷径

- 禁止仅把 `repo.login()` 改成无条件覆盖；这会让任何误输入用户名的人静默踢掉正在工作的用户。
- 禁止只在 `login_required` 检查 SID；它无法封住检查后、提交前发生的接管竞态。
- 禁止让 heartbeat 无条件续 idle；否则 30 分钟无操作退出仍然是假的。
- 禁止在 takeover/timeout 时删除 assignment。
- 禁止遇到 401 立刻导航，必须先保护本地脏数据。
- 禁止失败重试生成新 `operation_id`。
- 禁止把 localStorage/IndexedDB 当作跨设备存储。
- 禁止依赖 unload 请求释放 session。
- 禁止在日志、API 或 IndexedDB 中存储 Flask 签名 Cookie 或原始 SID。

## 21. 参考依据

- [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)：idle、absolute、renewal timeout 及服务端失效原则。
- [Kubernetes Lease](https://kubernetes.io/docs/reference/kubernetes-api/coordination/lease-v1/)：holder、renew time、lease duration 和并发接管模型。
- [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html)：轮换旧凭证、重放检测和撤销思路。
- [MDN `beforeunload`](https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeunload_event)：关闭事件并不可靠。
- [MDN IndexedDB](https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API/Using_IndexedDB)：浏览器持久化离线结构化数据及其边界。
