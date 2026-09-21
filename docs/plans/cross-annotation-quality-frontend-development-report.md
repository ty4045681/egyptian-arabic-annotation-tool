# 交叉标注质检前端开发报告

日期：2026-09-20\
分支：`0918`\
计划：`docs/plans/2026-09-20-cross-annotation-quality-frontend-plan.md`

## 交付范围

在已有交叉质检后端上完成前端闭环，未新增数据库迁移，未改候选池、抽样算法、词差异、发布归属或导出规则。页面文案为英文。

1. 标注工作台继续使用 `Claim next task`，按 `mode === "cross_check"` 进入盲标。
2. `completed.html` 增加 `My cross-checks` 只读记录。
3. 管理员侧边栏增加 `Cross-checks`：抽样配置、全时段队列、对照审阅、三种裁定和取消进行中轮次。
4. Overview / Quality 增加全时段摘要和训练导出说明。
5. Playwright 浏览器验收与截图。

## 改动文件

| 文件 | 作用 |
| --- | --- |
| `static/cross-check.js` | 状态/原因标签、bps 转换、Unicode code point 差异高亮 |
| `static/admin-cross-check.js` | 管理员队列、配置、对照、裁定、取消 |
| `index.html` | 复标标识、提交/释放文案、outbox flash、迟到响应隔离 |
| `completed.html` | 双标签、本人复标列表与只读详情、`cross_check_active` 提示 |
| `admin.html` / `admin.js` / `admin.css` | 导航、Overview/Quality 摘要、Cross-checks 页、日期工具条隔离 |
| `annotation_metadata/routes.py` | `ALLOWED_STATIC` 增加 `cross-check.js`、`admin-cross-check.js` |
| `tests/browser/test_cross_check_*.py` | 工作台、历史、管理员、差异高亮 |
| `tests/browser/cross_check_helpers.py` | 造数与登录 helper |
| `tests/browser/conftest.py` | `cross_check_site` fixture |
| `tests/browser/test_english_platform_acceptance.py` | 覆盖新 Cross-checks 视图的英文检查 |

未修改上一轮未提交的后端修复文件（`annotation_repository.py`、`annotation_quality/claiming.py` 等）。

## 页面入口

- 标注：`/`，`POST /api/assignment/claim`，复标标识 `Independent annotation`
- 提交成功：`completed.html?tab=cross-checks&round=<uuid>`
- 本人记录：`GET /api/cross-checks/mine`、`GET /api/cross-checks/<round_id>/submission`
- 管理员：`/admin?view=cross-checks&state=awaiting_review&round=<uuid>`
- 抽样：`GET/PUT /api/admin/cross-check-settings`（现有 CSRF，无 admin key）
- 队列/详情/裁定/取消：`/api/admin/cross-checks` 及其 round 子路由
- 音频：标注员 `/api/audio/<task_id>`，管理员详情返回的 `audio_url`

## 实际请求与行为要点

- 复标判断只用 `mode === "cross_check"`，不按 `status`。
- 提交反馈以 `published` 和 `cross_check.state` 为准；不提示复标稿已发布。
- 释放复标文案：`Release this cross-check? This round will close and your work will not be submitted.`
- 差异高亮用 `Array.from` 按 Unicode code point 切片，不先 NFC/trim。
- 词差异率 `null` 显示 `Not available`，显示用 `rate × 100`，是否超阈值只看后端 state/reason。
- 概率输入按字符串转 bps：`10% → 1000`，`0.01% → 1`，`100% → 10000`。
- original/secondary 裁定请求不带 base/segments/admin_key。
- edited 发送完整 base 片段白名单字段。
- 交叉质检列表默认全时段，不继承控制台 Last 30 days；离开后恢复原日期。

## 测试证据

环境：uv、PostgreSQL 16.2（pgserver）、Playwright Chromium headless。

```
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync pytest -q \
  tests/browser/test_cross_check_workspace.py \
  tests/browser/test_cross_check_history.py \
  tests/browser/test_cross_check_admin.py \
  tests/browser/test_cross_check_diff.py \
  tests/browser/test_english_platform_acceptance.py
# 19 passed

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync pytest -q \
  tests/test_cross_check_*.py \
  tests/browser/test_scene_workflow.py \
  tests/browser/test_regression_offline_retry_backoff.py \
  tests/browser/test_login_dashboard.py \
  tests/browser/test_session_takeover.py
# 144 passed

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync pytest -q \
  tests/browser/test_regression_scope_*.py \
  tests/browser/test_regression_review_only_save.py \
  tests/browser/test_regression_english_reopen_display.py
# 7 passed
```

截图（合成数据）在 `docs/plans/cross-annotation-quality-frontend-acceptance/`：

| 文件 | 内容 | viewport |
| --- | --- | --- |
| `workspace-submitted-awaiting.png` | 复标提交 awaiting review | 1366×768 |
| `workspace-submitted-passed.png` | 复标自动通过 | 1440×1000 |
| `workspace-cross-check-restored.png` | 刷新恢复本人草稿 | 默认 |
| `history-cross-check-detail.png` | My cross-checks 只读详情 | 1366×768 |
| `admin-awaiting-queue.png` | 全时段待审队列（含 40 天前轮次） | 1440×1000 |
| `admin-unicode-compare.png` | Unicode/emoji 对照与 REPLACED 标签 | 1440×1000 |
| `admin-unicode-arabic-mobile.png` | 窄屏对照 | 390×844 |
| `admin-decision-secondary.png` | 采用复标 | 1366×768 |
| `admin-decision-edited.png` | 编辑后发布 | 1440×1000 |
| `admin-decision-conflict.png` | 两名管理员竞争后显示最新结果 | 默认 |

## 未完成事项与限制

- 后端 10 万条容量验收仍未做；本轮浏览器大列表/3000 词详情只证明 UI 可打开和导航，不宣称容量验收完成。
- 音频 fixture 原先按 filename 写入，但任务实际包含 folder，导致音频接口返回 404；复核已改为按 rel_path 写入。同一 WAV 在正确路径下可由 Headless Chromium 正常播放。浏览器用例现增加加载成功、差异跳转播放及退出停止的断言。
- 首版不做预先双人分配、第三人投票、按人惩罚、批量自动裁定、前端重算差异、训练包生成器。
- 个人 `My cross-checks` 列表没有文件名/时长（后端未提供），只显示短 task id、状态、提交时间。
- 生产抽样开关本轮代码不会自动改写；测试只在隔离库把抽样设为 100%/指定 bps。
- 管理员编辑稿只存在当前页内存，不接入标注员 IndexedDB。

## 已知产品口径

- `passed` 表示比较通过，原稿仍是当前发布版本。
- `training_export_blocked=false` 只表示该轮次不再阻塞，不等于所有导出条件满足。
- 公共排行榜累计时长仍用后端口径，浏览器不把复标劳动量加进总标注音频。


## 复核修复（2026-09-20）

- 裁定请求结果未知时保持输入冻结，允许以同一 operation_id 和完整 body 重试；暂停会丢弃请求快照的页面切换与刷新。明确的 400 校验失败则允许修订并创建新操作。
- 409 后保留可复制的裁定稿（含原因、完整片段和场景覆盖），独立展示最新轮次结果；服务器详情重读失败时仍可复制。拒绝丢弃后保留当前可编辑内容。
- 抽样配置冲突保留开关、原始概率输入和原因，单独展示服务端值；读到新 revision 后等待管理员再次保存。重读失败时禁止保存旧 revision，并允许重读恢复。
- My cross-checks 切换标签时释放失效请求的加载锁，旧响应不覆盖新列表。
- 补齐 ADMIN_KEY 导入和测试包声明，解决缺失常量以及前后端同名测试模块的收集冲突。
- WAV fixture 使用任务 rel_path，工作台、本人记录和管理员详情均检查音频可加载；管理员差异导航检查实际播放和退出停止。

新增 `tests/browser/test_cross_check_regressions.py` 的 9 个回归用例覆盖请求丢失、响应丢失、400 后修订、409 稿件复制、冲突重读失败、拒绝丢弃、配置冲突及标签切换时的迟到响应。

复核后的验证（PostgreSQL 16.2、Playwright Chromium headless）：

```bash
UV_CACHE_DIR=/tmp/annotation-review-uv-cache \
UV_PYTHON_INSTALL_DIR=/opt/annotation-python \
uv run --no-sync pytest -q --tb=short
# 486 passed, 1 skipped in 643.54s
```

唯一跳过项是默认关闭的 `test_speed_query_plan_on_100k_current_versions`，需显式设置 `ANNOTATION_SPEED_EXPLAIN=1`。本次未进行 10 万条容量验收。

配置恢复逻辑最后补充验证：冲突后读取失败，管理员继续修改概率和原因，再次读取必须保留最新输入。以下用例另外运行通过：

```bash
UV_CACHE_DIR=/tmp/annotation-review-uv-cache \
UV_PYTHON_INSTALL_DIR=/opt/annotation-python \
uv run --no-sync pytest -q \
  tests/browser/test_cross_check_regressions.py::test_settings_conflict_preserves_inputs_until_explicit_resave \
  tests/browser/test_cross_check_admin.py::test_sampling_settings_percent_to_bps --tb=short
# 3 passed in 17.12s
```
