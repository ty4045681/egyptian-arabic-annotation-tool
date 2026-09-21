# 交叉标注质检前端实现计划

日期：2026-09-20\
目标分支：`0918`\
执行对象：grok build\
交付范围：标注端、个人复标记录、管理员抽样配置与质检工作台、统计和导出提示，以及浏览器验收。

## 1. 目标与范围

在已经实现的后端交叉质检能力上完成用户可操作的前端闭环：

1. 标注员继续使用 `Claim next task`，按所选场景领取普通任务或独立复标任务；历史已标注音频也可能被抽到。
2. 复标者只看到自己的独立稿件，能够保存、提交、释放，并在提交后查看自己的只读记录。
3. 管理员能设置是否抽样及抽样概率，查看待审任务，并通过文本对照和音频完成裁定。
4. 严格超过 10% 的词差异，以及跳过、空文本、Bad Quality 冲突或比较异常，按照后端返回的状态进入人工质检。
5. 总标注音频时长按音频去重；复标劳动量独立展示；让用户理解质检中的音频仍计入已标注成果，但暂时不能进入训练导出。

本轮以现有接口完成前端，不新增数据库迁移，不改变候选池、随机概率、词差异算法、发布归属、导出选择或统计规则。新增页面使用现有原生 HTML/CSS/JavaScript，不引入 React/Vue、站点构建平台或新的打包链。

页面的按钮、提示、空状态、无障碍标签继续使用**英文**；本计划使用中文说明。阿语、英语等标注内容按原文呈现，不翻译、不规范化后覆盖原文。

首版不做预先分配两个标注员、第三人投票、重复质检轮数设置、按人惩罚/评分、批量自动裁定、前端重新计算差异率、训练包生成器或管理员离线自动发布。

## 2. 实现前必须确认的代码基线

### 2.1 保留当前后端修复

编写本计划时，`0918` 的已提交 HEAD 为 `43351aa`，工作区还有上一轮已完成并验证的修复。执行时以**实际最新工作区**为准，不能只从旧 HEAD 创建副本而漏掉修复，也不能覆盖已有未提交文件。

必须包含以下后端行为及回归测试：

| 已修复事项 | 代码位置 | 验证位置 |
| --- | --- | --- |
| 互相领取不会因原作者外键锁死锁，管理员持锁时可退让 | `annotation_repository.py::_lock_active_annotator`、`annotation_quality/claiming.py::try_claim_cross_check` | `tests/test_cross_check_concurrency.py` |
| 质检标签绑定实际发布版本，新修订不继承旧 verdict | `annotation_quality/queries.py` | `tests/test_cross_check_export.py` |
| 差异字符位置指向未经修改的原始文本 | `annotation_quality/comparison.py` | `tests/test_cross_check_comparison.py` |

上轮验证结果：PostgreSQL 16.2 完整测试 `459 passed, 1 skipped`；PostgreSQL 18.6 相关三组测试 `42 passed`。这只是实施前基线，不是本轮前端验收结果。

相关文档：

- [后端实现计划](2026-09-18-cross-annotation-quality-backend-plan.md)
- [后端开发报告](cross-annotation-quality-backend-development-report.md)
- [会话接管与离线恢复计划](session-takeover-offline-recovery-implementation-plan.md)
- [场景溯源实现计划](scene-provenance-implementation-plan.md)

后端报告注明的交叉质检 10 万条容量验收仍未完成。本轮要验证浏览器大列表/长文本表现，但不能用这些浏览器结果宣称后端容量验收完成。

### 2.2 现有前端接入位置

| 文件/入口 | 当前职责 | 本轮改动 |
| --- | --- | --- |
| `index.html` | 场景选择、领取、音频/片段编辑、保存提交、历史预览 | 复标模式、提交反馈、释放文案、恢复与冲突处理 |
| `completed.html` | 本人当前已发布成果、查看和 Correct | 增加 `My cross-checks` 标签页及独立只读详情 |
| `static/offline-drafts.js` | IndexedDB 草稿和不可变请求 outbox | 必要时补充 mode/round 元数据，保留旧草稿兼容与身份校验 |
| `static/annotator-session.js` | 会话冻结、接管、离线提示 | 复用既有机制，保证新入口受同样保护 |
| `static/metadata.js` / `.css` | 场景目录、来源说明、场景复核控件 | 复用；复标不引入他人参考核验或预测 |
| `admin.html` / `admin.js` / `admin.css` | 管理员导航、会话/CSRF、Overview、Quality、任务详情 | 新增交叉质检视图、配置、对照和裁定；接入统计 |
| `login.html` / `static/login-dashboard.js` | 公共排行榜和累计标注时长 | 保持后端去重口径，必要时修正文案，不增加复标积分 |
| `tests/browser/` | 真实 Flask + 临时 PostgreSQL + Playwright | 新增完整交叉质检浏览器用例，并跑既有回归 |

建议新增 `static/cross-check.js` 承载纯状态标签、数值格式、差异渲染等公共辅助；较大的管理员视图放入 `static/admin-cross-check.js`。采用与现有代码兼容的命名空间或显式初始化接口，避免全局 `$`、`state` 等重名。管理员 API 客户端由 `admin.js` 注入，不另存管理员密钥。

`index.html`、`completed.html` 已有内联脚本。只抽取本功能需要的逻辑，不同时重构整个工作台。新增资源保持同源，检查脚本加载顺序，并按现有方式更新被修改资源的版本参数。

## 3. 页面结构与导航

### 3.1 标注端

- 工作台保留一个 `Claim next task` 入口和现有场景选择器。前端不掷随机数，不要求选择“我要复标”，不增加第二个领取接口。
- 当前复标任务在任务栏显示小型 `Independent annotation` 标识和说明，保留原有音频、ASR、文本、Bad Quality、时间调整、场景复核与保存操作布局。
- `completed.html` 增加两个平级标签页：`Completed data` 和 `My cross-checks`。普通成果仍使用原接口和统计；复标记录单独加载。
- 复标提交成功后给出到 `completed.html?tab=cross-checks` 的入口；支持 `round=<uuid>` 直接打开本人只读提交。

### 3.2 管理员端

- 侧边栏增加 `Cross-checks`，建议 `data-view="cross-checks"`；保留现有 `Quality`，因为其速度异常、Bad Quality、长期未完成等内容仍然有效。
- Overview 和原 Quality 页增加交叉质检摘要入口，跳转到新视图的 `Awaiting review` 队列。
- 新视图包含：队列摘要、筛选/列表、`Sampling settings` 按钮、详情审阅区域。详情采用当前页面内的大面积审阅区域，保留回到列表的筛选和滚动位置。
- 建议地址：`/admin?view=cross-checks&state=awaiting_review&round=<uuid>`。刷新、返回、前进后能够恢复所选视图/轮次；URL 不保存标注文本、裁定原因或密钥。
- 交叉质检列表默认**全部时间**，不继承控制台默认的 Last 30 days；其时间筛选明确表示轮次创建时间。进入旧视图后恢复原来的日期选择。

### 3.3 视觉与交互约束

- 沿用标注端现有深色工作台和管理员现有浅色控制台，不统一改版。
- 宽屏支持两栏原稿对照；窄屏改为上下排列或可切换的双方稿件，保留同一个音频播放器。
- 状态由文字和颜色共同表达；差异同时有操作标签，不能只用红/绿区分。
- 所有按钮、筛选、差异跳转、弹窗可用键盘操作；对话框有焦点管理，关闭后返回触发按钮。异步结果使用适度的 `aria-live` 提示。
- 1440×1000、1366×768、390×844 均需检查；页面不产生无意义的横向溢出，局部表格可受控滚动。

## 4. 状态和业务含义

状态以服务器为准，不能根据前端计算出的百分比、任务的 `annotated` 状态或按钮点击结果自行推断。

| `cross_check.state` | 英文标签 | 管理员可操作内容 | 该轮次造成训练导出阻塞 |
| --- | --- | --- | --- |
| `in_progress` | In progress | 查看；填写原因后取消 | 是 |
| `awaiting_review` | Awaiting review | 采用原稿、采用复标、编辑后发布 | 是 |
| `passed` | Passed | 只读查看 | 否 |
| `adjudicated` | Adjudicated | 查看裁定结果、原因与原始双方证据 | 否 |
| `cancelled` | Cancelled | 只读查看终止原因 | 否 |
| `invalidated` | Invalidated | 只读查看失效原因 | 否 |

注意：

1. `passed` 表示这次比较通过，后端仍保留原稿为当前发布版本；不能提示复标稿已发布。
2. `adjudicated` 不等于始终可训练：管理员可能采用 skipped 结果。
3. `training_export_blocked=false` 只说明该轮次不再形成质检阻塞，不等于所有导出条件均满足。
4. 旧轮次详情是历史证据。若 `current_published_version_id` 已经不同于该轮次覆盖的版本，提示 `The published version has changed since this review.`；不能把历史通过标签贴到新版本。
5. `awaiting_review` 不能取消，也不会因关闭抽样、退出登录、浏览器关闭或等待超时而自动放行。
6. `in_progress` 的复标稿尚未提交，管理员侧必须标为未完成，不能作为最终差异结果呈现或用于裁定。

## 5. 标注工作台详细改动

### 5.1 领取与盲标

- 继续 `POST /api/assignment/claim`，提交现有 `source_scene` 等选择条件。按响应 `mode === "cross_check"` 判断复标，不按 `status` 判断是否可编辑。
- 复标响应中的 `status` 可能是 `annotated`，但 `version_id` 是本人的可编辑草稿；`assigned`、`mode`、`lease_token`、`revision` 和 `cross_check` 应保留在状态中。
- 草稿初始内容完全来自领取/恢复响应。新领取通常文本为空；恢复任务可以有本人已保存的文本，不能为“保持盲标”而再次清空。
- 保留后端提供的原始 ASR 参考；禁止将 ASR 自动复制到标注文本，也不能请求他人的发布稿来补全草稿。
- 不显示他人的文本、用户名、原稿版本、Bad Quality、场景复核、预测或比较指标；不能仅用 CSS 隐藏已经请求到的内容。
- 渲染新任务时清理旧任务 DOM、详情缓存、播放位置、核验参考和编辑状态，避免前一任务或同浏览器管理员页面的数据残留。
- `GET /api/assignment` 恢复当前任务，仍然优先于新的场景选择及抽样配置。已有复标不会因关闭抽样而被客户端丢弃。
- 空池提示以服务端 `pool.reason` 和 `available` 为准。普通 pending 为 0、但 `cross_check_available > 0` 时仍应显示领取入口；`pool.available` 已包含两个池，不能再次相加。
- `pool.by_scene` 已按场景合并可领取数量；继续使用现有场景选择器和同一十场景目录。

### 5.2 保存、提交、释放

保存和提交复用当前排队保存机制，不建立另一套请求生命周期：

| 动作 | 接口 | 约束 |
| --- | --- | --- |
| 读取/恢复 | `GET /api/assignment` | 不能使用 `GET /api/assignment/current`，后者没有 GET 路由 |
| 保存 | `PATCH /api/assignment/current` | 沿用租约、revision、operation_id、脏片段和 scene_review |
| 完成/跳过 | `POST /api/assignment/current/complete` | 先处理已有保存队列，再以同一请求快照重试 |
| 释放 | `POST /api/assignment/current/abandon` | 沿用 lease_token、operation_id、confirm |

实现重点：

- `submitTask()` 必须读取成功响应的 `published` 和 `cross_check.state`。普通标注保留原反馈；复标统一以“已提交”表述，不提示新增了一条音频或复标稿已发布。
- 建议反馈：`Cross-check submitted — passed.` 或 `Cross-check submitted — awaiting admin review.`。跳过复标同样读取返回状态，不提示原音频已被整体跳过。
- 提交确认前不清除编辑内容和 outbox；确认后再清理该租约草稿、刷新统计并显示当前池状态。
- 完成按钮保留现有必填校验；无法标注的片段可标 Bad Quality，整条无法处理可使用现有 Skip。不要为了覆盖后端空文本异常而允许普通用户误提交空白稿。
- 释放复标须使用专用文案，例如 `Release this cross-check? This round will close and your work will not be submitted.`。不能沿用普通任务“saved draft will remain，可继续领取”的承诺。
- 不同按钮不能并发提交同一租约。处理响应期间禁用重复点击，但 GET 刷新、音频控制等无关操作按实际需要保留。

复标提交响应关键形状：

```json
{
  "success": true,
  "task_id": "11111111-1111-4111-8111-111111111111",
  "status": "annotated",
  "published": false,
  "cross_check": {
    "round_id": "33333333-3333-4333-8333-333333333333",
    "state": "awaiting_review",
    "training_export_blocked": true
  }
}
```

### 5.3 离线恢复、会话接管和迟到响应

- 继续使用 `static/offline-drafts.js` 的 IndexedDB 和不可变 outbox，不用 localStorage 另建一套文本草稿。
- 当前草稿存储键是 username + task_id，恢复必须继续核对 username、version_id、lease_token、server_revision；可增量保存 mode、round_id 辅助识别，但不能将这些附加元数据塞入严格校验的请求 body。
- 兼容旧 IndexedDB 记录：旧记录缺少 mode/round_id 时，仍须满足版本与租约校验；不按 task_id 单独恢复，不批量清空用户草稿。
- 完成请求如果服务器已接受但响应丢失，使用原 operation_id 和完全相同的 body 重放；不得修改旧 outbox、生成新 operation_id 后再次发布。
- `replayOutbox()`/恢复后的结果提示也识别复标成功，不能只修直接点击提交的路径。
- 401、账号停用、会话被接管继续调用现有冻结机制。409/403 租约或轮次失效时保存本地文本，显示冲突/导出本地文本/重新读取任务入口，停止无效自动重试。
- 部分旧错误只有 HTTP 状态及 message，没有稳定 code。先按 code 分类，有关租约的通用 409 可重新 GET 当前任务确认；不依赖英文错误全文的字符串匹配。
- 迟到的保存、提交或历史详情响应，只有仍属于当前用户和同一个任务/版本/租约时才能更新编辑器。不能把旧响应写进新领取的任务。
- 返回历史记录或切换标签不能隐式释放当前复标任务；继续保证退出/关闭页面时的脏数据保护。

## 6. 我的复标记录

### 6.1 数据源和展示边界

| 需求 | 接口 | 当前返回形状 |
| --- | --- | --- |
| 复标记录列表 | `GET /api/cross-checks/mine?limit=25&cursor=…` | `items`、`next_cursor` |
| 本人提交详情 | `GET /api/cross-checks/<round_id>/submission` | round/task/version、state、submitted_at、target_status、segments、skip_reasons、review |
| 音频 | `GET /api/audio/<task_id>` | 沿用已授权的本人音频入口 |

- 列表只返回**已提交**的本人复标。进行中的任务在工作台恢复；不要在列表空状态中误称进行中的稿件丢失。
- 列表 item 当前只有 round_id、task_id、state、submitted_at、version_id；没有 filename、duration、总记录数、差异率或是否被采用。
- 首版使用提交时间、状态、简短音频编号和 `View submission` 操作。缺少文件名/时长就不展示该列，不能填 0 或逐条调用普通成果详情补全。
- 只显示 `N loaded`，不能把当前分页长度当作总记录数；不提供后端未支持的全局状态筛选、搜索或全量个人复标时长。
- 使用独立 cursor、加载状态和请求序号；切换标签后旧列表响应不能覆盖新标签。按钮属性与现有 `[data-view]` 普通详情绑定隔离，建议 `data-cross-check-round`。

### 6.2 只读详情

- 展示本人原始提交文本、片段时间、Bad Quality、跳过原因、本人场景核验及提交状态；按 task_id 构造已授权音频地址。
- 禁用文本/时间编辑、保存、完成、跳过、释放、Correct 及相应快捷键；历史详情不会创建新的 assignment。
- 不通过 `/api/completed/<task_id>` 读取复标稿，因为它代表本人当前已发布成果，可能拒绝访问，也可能已是另一版本。
- `adjudicated` 只显示“管理员已处理”；本人接口没有裁定方向/原因，不能推断“你的稿件被采用/被拒绝”，也不能请求管理员详情补字段。
- 即使复标后来被采用，`My cross-checks` 仍然是只读证据。若这份稿件成为本人当前发布成果，原 `Completed data` 的 Correct 流程按既有后端权限处理，两种入口不混同。
- 原稿作者在交叉质检开放期间点击普通 Correct 可能收到 `cross_check_active`：解释 `This audio is under cross-check. It cannot be corrected until the review is complete.`。现有 completed 列表没有开放轮次字段，不做逐条探测或假装能预先判断所有按钮。
- 关闭详情停止播放、清理内容并归还焦点。404、账号变化或授权失败时清除上一份详情，不能残留旧用户文本。

## 7. 管理员抽样设置

### 7.1 交互

- 入口位于 Cross-checks 页的 `Sampling settings`。加载成功后才启用保存；配置加载失败不以默认值覆盖服务端。
- 控件：启用开关、抽样概率百分比输入框、只读 `Review threshold: word difference > 10%`、变更原因、保存按钮。
- 概率范围 0–100%，最多两位小数；准确转换为整数 bps，`10% → 1000`、`0.01% → 1`、`100% → 10000`。建议从输入字符串转换，拒绝额外小数，不依赖浮点误差容忍。
- 文案明确这是新领取时优先尝试复标池的概率；另一池为空时后端有回退，因此不能承诺实际复标占比严格等于该百分比。
- 关闭或 0% 只影响新复标领取，进行中和待审任务仍可处理。不为关闭抽样增加“清空待审”或“全部放行”行为。
- 操作原因必填，去掉首尾空白，长度不超过 4000 个 Unicode 字符；设置未变化时不生成无意义的写请求。
- 新接口只需要现有管理员会话和 CSRF，不要求重新输入管理员 key；不能照搬停用账号/批量撤销的额外 key 字段，否则会被严格模型拒绝。

### 7.2 请求

`GET /api/admin/cross-check-settings` 返回 enabled、sampling_rate_bps、revision、只读阈值及比较版本、更新时间。

```http
PUT /api/admin/cross-check-settings
Content-Type: application/json
X-CSRF-Token: <current-session-token>
```

```json
{
  "operation_id": "44444444-4444-4444-8444-444444444444",
  "expected_revision": 0,
  "enabled": true,
  "sampling_rate_bps": 1000,
  "reason": "Enable cross-check sampling at 10 percent"
}
```

管理员 `api` 已有通用 `request()`，可增加 `put()` 包装或显式传 `method: "PUT"`，复用同源凭证与 `X-CSRF-Token`。不发送只读阈值、updated_at 等响应字段。

409 `stale_revision` 时保留用户输入并重新读取服务端配置，显示双方值，由管理员确认后以新 revision 发起新操作，不能自动覆盖别人刚保存的配置。

## 8. 管理员队列

接口：`GET /api/admin/cross-checks`。

| 参数 | 首版 UI | 注意事项 |
| --- | --- | --- |
| `state` | 六种状态切换，默认 awaiting_review | 不传 `all`，后端没有全状态聚合值 |
| `source_scene`、`batch_code` | 来源场景、批次 | 复用 `/api/admin/metadata/facets`，使用来源口径 |
| `original_annotator_id`、`secondary_annotator_id` | 分开的原标注员/复标员选择 | 使用 ID，复用已加载的管理员人员目录 |
| `reason_code` | 待审原因 | 仅使用后端固定枚举 |
| `q` | 文件名、路径或完整音频/轮次 ID 搜索 | 与 `annotation_quality/queries.py::list_where_sql` 一致；UUID 为完整值精确匹配 |
| `from`、`to`、`timezone` | 轮次创建时间范围 | 开始包含、结束不包含；包含某日的 UI 结束日期转换为下一天 |
| `limit`、`cursor` | 每页 50 / Load more | limit 最大 100；cursor 原样传递 |

返回 `items`、`next_cursor`、`applied_filters`，没有过滤后总量。列表列建议：音频编号、双方标注员、音频时长、状态、词差异率、原因、创建/提交时间、查看操作。

- 列表当前没有 filename；使用简短 task_id，详情才展示 filename。名字从现有人员目录映射，缺失时显示简短 ID，不能发起每行补查。
- 筛选变化时清空旧 cursor 和旧选择；搜索防抖约 300ms。AbortController/请求序号必须阻止旧响应覆盖新筛选。
- 分页按 round_id 去重；只显示已加载数量。全局队列摘要必须标明 `All-time, all scenes`，不冒充当前筛选的总数。
- 新列表构建独立参数，不直接使用给旧看板注入默认日期、bucket、annotator_id、signal 的 `commonQuery()`。
- 配置、目录、列表各自展示加载/失败/重试；目录失败可以退回 ID 展示，不能因此删除筛选值或提交空目录造成的设置变化。
- 需要空列表、首次加载失败、追加分页失败、失效 cursor、会话过期等状态。失效 cursor 重新加载第一页，保留筛选；追加失败保留已加载记录。

待审原因英文展示：

| code | 标签 |
| --- | --- |
| `word_difference_exceeded` | Word difference exceeds 10% |
| `submission_status_conflict` | Submission status differs |
| `empty_original_text` | Original transcript is empty |
| `empty_secondary_text` | Cross-check transcript is empty |
| `bad_quality_conflict` | Bad quality coverage differs |
| `comparison_unavailable` | Comparison unavailable |

允许多个原因同时显示。比率为 null 时显示 `Not available`，不得转成 0%；正常值按 `rate × 100` 显示。采用本功能专用格式化函数，不用会猜测输入是 0–1 还是 0–100 的通用 `formatPercent()`。四舍五入只用于显示，是否超阈值始终由后端 state/reason_codes 决定。

## 9. 管理员对照与音频审阅

### 9.1 详情结构

`GET /api/admin/cross-checks/<round_id>` 提供双方完整原始片段、场景核验快照、统计与 diff_ops、round revision、版本标识、audio_url 和裁定记录。

审阅区域依次展示：

1. 文件名、音频时长、轮次状态、触发原因、导出质检阻塞提示。
2. 双方词数、编辑距离及差异率；比较不可用时展示原因，不能把空值显示为通过。
3. 一个音频播放器，以及 `Previous difference` / `Next difference` 导航。
4. 原标注和复标两个独立的原始文本区域，含各自片段时间、Bad Quality、提交状态与跳过原因；场景核验以只读卡片展示。
5. 裁定操作区域或已完成裁定记录。技术 ID、比较版本、采样配置快照放在可展开的审计详情中。

`original_version_id` / `secondary_version_id` / round `revision` 属于此次详情快照，应与页面数据绑定，不能从列表或当前任务的另一个版本取代。

### 9.2 差异高亮的准确实现

后端 `diff_ops` 项目形状：

```json
{
  "op": "replace",
  "original_word": "cat",
  "secondary_word": "dog",
  "original": {
    "segment_id": 1,
    "text_start": 5,
    "text_end": 8,
    "start_s": 0.0,
    "end_s": 5.0
  },
  "secondary": {
    "segment_id": 10,
    "text_start": 4,
    "text_end": 7,
    "start_s": 0.0,
    "end_s": 5.0
  }
}
```

对应示例原文是 `e\u0301 😀 cat`，复标原文是 `é 😀 dog`。位置是**各自原始片段内 Unicode code point 的半开区间 `[text_start, text_end)`**，不是 UTF-16 索引、UTF-8 字节数或词序号。

实现要求：

- 用 `Array.from(rawText)` 按 code point 切片，或构建等价的 code point → UTF-16 索引表；不能直接用原始字符串的 `slice(start, end)` 套用后端位置。
- 高亮基于 `segments[].text` 原文。不能先 NFC/casefold、trim、替换标点或合并空白再切片，也不能把 normalized word/summary 当作展示原文。
- 用“侧别 + segment_id”定位，保留双方各自的分段。不能按左右数组下标、相同片段 ID 或相同切分假设拼行。
- `match` 不做差异强调；`replace` 标记双方；`delete` 仅原侧有映射；`insert` 仅复标侧有映射。null 映射显示缺失侧占位，不制造假的时间点。
- 同一片段先收集并整理区间，再一次性渲染文本节点和 `<mark>` 等安全节点；处理重复/重叠范围，不能重复展示原文或拼接未经转义的 innerHTML。
- 阿语使用适当的 `dir="auto"`、bidi 隔离和保留换行/空白的样式；时间、编号、百分比保持 LTR。RTL 不反转逻辑字符下标。
- 差异导航按后端操作顺序跳转到双方对应片段，展开必要内容并移动可见焦点。
- 缺失片段、越界/非法范围或无法恢复的历史映射时，保留完整原文并降级为片段级标记，提示精确高亮不可用，不静默显示错位词语。
- `comparison_unavailable` 或没有有效 diff 时，仍可听音频、看全文和裁定；不在浏览器运行另一套 Levenshtein 作为替代结论。
- Bad Quality 对照使用各自片段及 `segment_map.original_bad_quality` / `secondary_bad_quality` 的毫秒区间，与词差异分开展示；不能只按片段 ID 判断一致。

### 9.3 播放行为

- 使用详情返回的管理员 `audio_url`，请求保持同源授权；不构造公开下载地址，不把管理员 key 放入 URL。
- 点击差异/片段时 seek 到其 `start_s`，可播放至 `end_s` 后暂停。这里是所属片段的时间，UI 不承诺逐词精确对齐。
- 单侧映射缺失时播放另一侧的有效片段；两侧都没有有效映射则只允许正常全文播放。
- 等待 metadata、处理音频加载/权限/播放失败；失败不清除审阅内容，不播放上一条音频。
- 切换轮次、关闭详情、退出登录时暂停旧播放器、解除监听并清理资源；不能左右两份稿件各自自动播放。

## 10. 管理员裁定与取消

### 10.1 裁定表单

只在 `awaiting_review` 展示可提交的三种选择，选择与结果区分明确：

| decision | 英文操作 | 结果 |
| --- | --- | --- |
| `original` | Use original | 保留原稿当前发布版本 |
| `secondary` | Use cross-check | 发布复标稿及其 annotated/skipped 结果 |
| `edited` | Edit and publish | 从所选 base 生成新的管理员裁定版本 |

- 裁定原因必填且去空白后不超过 4000 个 Unicode 字符。
- 最终提交前在同一表单呈现所选稿件、结果状态、原因、对训练导出的影响，并以 `Confirm decision` 明确提交；不增加与接口无关的管理员 key 输入。
- 不默认勾选某位标注员为正确，不因差异率高低自动作出裁定。

采用原稿/复标请求示例：

```json
{
  "operation_id": "55555555-5555-4555-8555-555555555555",
  "expected_revision": 1,
  "expected_original_version_id": "66666666-6666-4666-8666-666666666666",
  "expected_secondary_version_id": "77777777-7777-4777-8777-777777777777",
  "decision": "secondary",
  "reason": "The second submission matches the audio"
}
```

提交到 `POST /api/admin/cross-checks/<round_id>/decision`。original/secondary 请求必须**省略** base、segments、target_status、skip_reasons、scene_review，不发送这些字段的 null 或空值占位。

### 10.2 编辑后发布

- 先选择 `Start from original` 或 `Start from cross-check`，深拷贝该侧 segments 成为独立编辑稿，原始两份证据继续只读。
- 首版编辑器支持逐段文本、Bad Quality、最终 annotated/skipped 状态、跳过原因以及可选的场景核验覆盖。保留所选 base 的完整片段 ID 集合和有效时间区间，不做片段拆分/合并或新的时间轴编辑器。
- 不把新编辑文本放进旧差异区间里高亮；原始对照和最终编辑稿是分开的区域。
- 切换 base 或离开有修改的编辑器时说明将丢弃的内容，并允许留下继续编辑；提交中冻结会改变请求内容的操作。
- annotated 时非 Bad Quality 片段必须有文本；skipped 时必须选择现有 `ALLOWED_SKIP_REASONS` 中的原因。
- 默认沿用 base 的场景核验，省略 scene_review；显式覆盖时使用现有场景目录/复核控件，只发送 status、scene_codes、note。目录加载失败时不能误提交空的场景覆盖。
- 用明确字段白名单构造 segments：id、start、end、duration、text、exclude_from_training；发送完整稿件，不发送少量 dirty 片段冒充完整裁定。

编辑请求示例（仅展示两段示例数据，真实请求须包含所选 base 的全部片段）：

```json
{
  "operation_id": "88888888-8888-4888-8888-888888888888",
  "expected_revision": 1,
  "expected_original_version_id": "66666666-6666-4666-8666-666666666666",
  "expected_secondary_version_id": "77777777-7777-4777-8777-777777777777",
  "decision": "edited",
  "base": "original",
  "target_status": "annotated",
  "skip_reasons": [],
  "segments": [
    {"id": 1, "start": 0.0, "end": 5.0, "duration": 5.0, "text": "Corrected transcript", "exclude_from_training": false},
    {"id": 2, "start": 5.0, "end": 10.0, "duration": 5.0, "text": "", "exclude_from_training": true}
  ],
  "reason": "Corrected the wording after listening to the audio"
}
```

管理员编辑稿首版保存在当前审阅页面内存中，不接入标注员 IndexedDB 或其 autosave。离开保护、手动复制/下载草稿及失败后保留内容必须可用；不能声称已经自动保存到服务器。

### 10.3 幂等、竞争和成功状态

- 在一次实际提交开始时创建 operation_id，冻结整个 body。网络失败/结果未知时只能重试同一 body 和 ID；未确认旧操作结果前，不允许以修改后的 body 重用 ID 或用新 ID 重复发布。
- 明确的校验失败可修改表单后创建新操作；409 stale_revision、cross_check_version_changed、cross_check_state_conflict、cross_check_active 时保留本地稿件，并重新读取该轮次确认最新状态。
- 两名管理员同时裁定时，后返回的冲突不能转成成功，也不能自动用新 revision 重试裁定。显示最新结果；本地编辑可复制/下载。
- 成功以接口返回的 state、final_version_id、final_status、training_export_blocked 为准。重新 GET 详情，刷新当前队列及摘要，保留筛选；不强制跳到下一条以免掩盖失败或丢失结果反馈。
- 原因和未提交文本不写入 URL、控制台日志或 clientlog。管理员注销时清理新视图缓存和文本。
- GET 请求可以随视图切换取消；POST 即使浏览器取消等待也可能已完成，不能把 AbortError 当作服务器回滚。

### 10.4 取消进行中轮次

仅对 `in_progress` 提供 `Cancel cross-check`，要求原因和明确确认：

```json
{
  "operation_id": "99999999-9999-4999-8999-999999999999",
  "expected_revision": 0,
  "reason": "Release an interrupted cross-check",
  "confirm": true
}
```

调用 `POST /api/admin/cross-checks/<round_id>/cancel`。说明该操作结束本轮并释放复标任务；不会发布复标内容。成功后刷新详情、列表和摘要。提交期间轮次已变为 awaiting_review 时按冲突处理，不能提供强制取消待审入口。

## 11. 统计与导出提示

### 11.1 字段映射

| UI 指标 | 数据源 | 展示口径 |
| --- | --- | --- |
| 工作台现有音频计数、累计时长 | `/api/dashboard` 的 stats | 服务器提供的音频成果统计 |
| 管理员累计标注时长 | `/api/admin/overview` 的 `totals.annotated_duration_seconds` | 维持接口当前筛选/去重口径 |
| 复标提交次数 | overview 的 `totals.cross_check_submitted_count` | 全局历史复标提交劳动量，独立展示 |
| 复标处理音频时长 | overview 的 `totals.cross_check_submitted_audio_seconds` | 全局历史复标劳动量，不加到总标注音频时长 |
| 进行中/待审/通过/已裁定轮数 | overview 或 `/api/admin/quality` 的 `cross_check` | `in_progress_count` / `pending_review_count` / `passed_count` / `adjudicated_count` |
| 被质检暂缓的音频时长 | `cross_check.blocked_audio_seconds` | 按 task 去重的全局阻塞时长 |
| 最早待审轮次创建时间 | `cross_check.oldest_pending_created_at` | 按字段含义展示，不冒充提交时间 |
| 公共排行榜累计时长 | `/api/leaderboard` 现有字段 | 保留后端口径，不在浏览器合并复标时间 |

当前 cross_check 摘要与复标劳动量是全局指标，不随旧看板的场景/日期筛选变化。新卡片明确标注 `All-time, all scenes`；不能与过滤后的时长直接相减得到所谓“可导出时长”。

新增复标统计优先放在管理员 Overview 和 Cross-checks 摘要。个人 `My cross-checks` 不使用全局复标劳动量冒充个人统计。

### 11.2 导出说明与现有列表

- 把“Annotated duration = currently usable”之类易误解的文案改为 `Published annotated audio`，说明正在复标/待审的数据仍计入该值。
- 新质检列表/详情明确展示 `Training export on hold`；正常总音频数、已标注总时长不因第二人提交翻倍。
- 质检解除后刷新服务端统计。不得用“旧时长 + 本次复标时长”做乐观更新，也不能用“已加载复标记录数”修改音频总量。
- 既有管理员 CSV 和 Excel/JSON 审计导出不等于训练包。新页面可显示：`Training exports exclude audio under cross-check or awaiting review. Audit exports may still include it.`
- 不在浏览器下载两份音频、拼接两份文本或自行过滤训练包；训练资格和实际发布版本由后端导出工具决定。
- 现有 `/api/admin/tasks`、`/api/completed` 并没有普遍提供单条 quality_state/training_eligible。本轮在新质检视图和全局摘要完成状态展示，不要求给所有旧任务行增加质检徽章；不拉取所有历史轮次再逐条关联，不能推断缺字段为“可训练”。
- 历史轮次通过、但音频后来修订的情况，沿用后端已经修复的版本绑定；前端不按 task_id 缓存“永远通过”。

## 12. 通用错误处理、数据隔离和性能

| 情况 | 页面行为 |
| --- | --- |
| 新接口 404/尚不可用 | 新区域显示功能不可用/重试；普通标注和原管理功能仍可使用，不写默认配置 |
| 401 或管理员会话过期 | 进入既有登录流程，清理敏感展示；提交结果未知时保留本次请求的状态，避免重复发布 |
| 403 CSRF | 提示会话/安全令牌失效，恢复合法会话后重试，不退回传 key 的旁路 |
| 400 输入错误 | 在对应字段显示错误，保留其他内容，不把失败当作空队列 |
| 409 revision/状态竞争 | 停止当前写入、保留编辑、刷新服务端快照，要求再次明确选择 |
| 网络失败或 5xx | 保留内容，提供受控重试；GET 失败不清空成功加载的数据，写请求沿用冻结操作 |
| 音频失败 | 单独提示，不影响文本读取，不继续播放旧音频 |

- API 文本、用户名、文件名、原因、标注稿均按不可信文本渲染；使用 textContent/DOM 文本节点，不把稿件当 HTML。音频只使用受控同源 API。
- 控制台、clientlog 和截图不要带管理员密钥/CSRF/真实生产标注内容；测试使用合成数据。
- 管理员详情按打开时加载，列表请求每页最多 100，不做每行详情请求。数据加载失败不会触发无限自动请求。
- 新队列可见且没有编辑/待确认写入时，至多每 30 秒刷新一次摘要；离开或隐藏页面暂停，不能后台刷新覆盖裁定输入。首版不需要 WebSocket。
- 长文本渲染按片段组织，必要时分批挂载或折叠；差异索引预先构建，音频 timeupdate 仅更新当前片段，不整页重渲染。
- 两侧各 3000 词的合成详情必须可打开、滚动和定位差异；记录浏览器、机器、渲染耗时与页面响应，不编造“逐词时间精度”。

## 13. 分阶段实施顺序

| 阶段 | 工作 | 完成标准 |
| --- | --- | --- |
| F0：基线与契约 | 确认工作区修复、路由、响应样本、UI 入口；建立合成浏览器 fixture | 没有遗漏未提交后端修复；接口字段与本计划核对完成 |
| F1：标注工作台 | mode 识别、盲标渲染、提交/释放反馈、outbox/会话兼容 | 普通和复标均可完成；恢复/断网/冲突不串稿 |
| F2：本人历史 | 双标签、分页、本人复标只读详情与音频 | 不调用普通成果详情读取复标；不允许修改历史复标 |
| F3：管理员队列/配置 | 新导航、队列筛选、设置表单、摘要 | 开关/概率正确保存；默认全时段待审；分页与失败恢复可靠 |
| F4：审阅/裁定 | 双稿对照、Unicode 高亮、播放器、三种裁定、取消 | 六状态权限正确；完整裁定闭环及并发冲突通过 |
| F5：统计/整体回归 | 统计口径文案、导出说明、可访问性、浏览器矩阵、交付报告 | 实测结果、截图、已知限制完整；既有流程无回归 |

每阶段保持可运行并完成对应验证；不把页面骨架、静态假数据或只成功一次的手工操作当作功能交付。运行中的抽样设置只在隔离测试库内调整，本轮代码实现不自动修改生产开关。

## 14. 验收与测试矩阵

### 14.1 测试方式

- 复用 `tests/browser/conftest.py` 的临时数据库、真实 Flask 服务、合成 WAV 和场景 fixture；使用真实管理员/标注员独立 browser context。
- 成功领取、保存、提交、裁定、版本/统计检查走真实后端。请求拦截只用于确定性地模拟延迟、断网、响应丢失、5xx 等异常，不能用 mock 成功响应代替端到端验收。
- 抽样命中测试在临时库设置 100%；关闭/0% 测试明确配置；10% 的真实随机分布由后端测试/容量工作验证，浏览器不写概率不稳定的抽样次数断言。
- 建议新增 `tests/browser/test_cross_check_workspace.py`、`test_cross_check_history.py`、`test_cross_check_admin.py`、`test_cross_check_diff.py`。复用 helper，避免每个用例复制一套登录/造数代码。

### 14.2 标注与个人历史

- [ ] 普通 pending 与历史 annotated 混合；所选场景匹配，普通池为空时可领取复标，复标池为空时普通任务仍可用。
- [ ] 首次领取空白稿与刷新恢复本人非空稿；开关关闭/场景改选后现有任务继续恢复。
- [ ] 预置原稿独有 secret 文本、原作者、Bad Quality、核验和预测，验证标注员网络响应/DOM/草稿均不含这些字段；测试同一浏览器跨账号缓存隔离。
- [ ] 保存正文、仅保存 scene_review、修改时间与 Bad Quality、普通完成、复标完成、复标 Skip 均符合原有表单校验。
- [ ] 相同文本自动 passed，11/100 词差异进入 awaiting_review，10/100 不按词差异入队；前端反馈不误称稿件已发布。
- [ ] 完成请求在服务器接受后丢失响应：原操作重放成功，仅一份复标提交，草稿确认后才删除。
- [ ] 离线恢复、自动重试退避、同账号会话接管、旧 lease/version 草稿、管理员取消/失效后再次保存均不会串稿或无限重发。
- [ ] My cross-checks 分页、空状态、错误重试、链接直达、本人音频、只读详情；不展示他人的内容/裁定方向，不出现 Correct。
- [ ] 标签切换和快速打开不同详情，迟到响应不能覆盖当前内容；切回工作台不释放未完成任务。
- [ ] 复标稿被采用后，My cross-checks 仍只读；原 Completed data 的合法纠正和开放质检阻止纠正均正确。

### 14.3 管理员

- [ ] 从 Overview/Quality 进入新队列；默认显示超过 30 天的待审轮次；原有 Quality 功能仍可用。
- [ ] 六种状态、场景、批次、双方人员、原因、创建日期、搜索、分页组合正确；没有发送 state=all 或混用 annotator_id。
- [ ] 目录/设置/列表分别失败；重试不会重置用户筛选或把默认配置写回服务器。
- [ ] 概率 0%、0.01%、10%、100% 正确；超范围/额外小数/空白原因被阻止；两个管理员修改配置产生 stale_revision 时不覆盖。
- [ ] 停止抽样后旧 in_progress、awaiting_review 仍能查看和处理。
- [ ] original、secondary、edited 三条裁定路径，包含采用 skipped 和编辑后 skipped；编辑提交包含完整 base 片段，原证据保留。
- [ ] 编辑 base 切换、离开提示、音频失败、scene 目录失败、仅覆盖场景核验等边界不丢失内容。
- [ ] 原/复标直接采用请求没有编辑字段，没有不支持的 admin_key 字段；写请求使用现有 CSRF。
- [ ] 两名管理员同时裁定，一方成功另一方明确冲突；网络丢响应重试不生成第二次裁定。
- [ ] in_progress 可取消；awaiting_review 无取消入口且服务端拒绝；取消后标注员不能继续提交旧租约。
- [ ] 已裁定后再出现新的当前发布版本，历史详情不会标记新版本已通过。

### 14.4 差异、统计、可用性

- [ ] match/replace/insert/delete；单侧 null；重复词；双方不同分段与相同 ID 不同位置。
- [ ] NFC 组合字符 `e\u0301`、阿语组合字符、Hangul Jamo、casefold 扩张、emoji/非 BMP 字符位于差异前；实际 `<mark>` 内容准确，原文无字符丢失/重复。
- [ ] 阿语/英语混排、空白和标点保留；XSS 样本作为普通文本；Bad Quality 区间差异单独呈现。
- [ ] 空文本、全部 Bad Quality、skipped、comparison_unavailable、历史非法映射均可审阅，不显示伪造的 0% 通过。
- [ ] 差异定位使用对应片段时间；播放停止、切换详情、关闭/退出后无旧音频残留。
- [ ] 同一条 60 秒音频被两人标注后，总标注时长仍为 60 秒；两条各 60 秒的音频为 120 秒；复标劳动量单独变化。
- [ ] 进行中/待审时呈现训练暂停，解除后刷新；审计导出与训练导出的文案不混淆，个人页面不使用全局劳动量冒充个人统计。
- [ ] 宽屏/小屏截图、键盘焦点、屏幕阅读器状态文案、全英文 UI、无 JS pageerror。
- [ ] 3000 词双侧详情、50/100 行列表：无逐行请求，无持续增长的监听/播放器，滚动和差异导航可操作。

### 14.5 执行与证据

在仓库既有 uv 环境执行，Python 与依赖管理沿用 uv。根据环境设置 Python/缓存路径，不安装与项目无关的前端工具链。

```bash
uv run --no-sync pytest -q tests/test_cross_check_*.py
uv run --no-sync pytest -q tests/browser/test_cross_check_*.py
uv run --no-sync pytest -q
```

可用原生 PostgreSQL 18 时，沿用 `ANNOTATION_PG_BINDIR` 或仓库 `scripts/run_pytest_with_postgres.py` 在隔离库复验新浏览器流程及受影响的交叉质检测试，记录实际版本。运行已有英文、场景复核保存、离线重试、会话接管、scope 编辑、普通历史纠正和累计时长浏览器回归；全套通过后不无理由重复跑同一组。

截图和报告建议放入 `docs/plans/cross-annotation-quality-frontend-acceptance/`，包含至少：复标工作台、复标提交后、本人历史、待审队列、Unicode/阿语对照、编辑裁定、竞争冲突、移动视图。只使用合成数据；记录截图所用状态、viewport、测试命令和真实结果。

## 15. 最终交付清单

- [ ] 五项前端范围全部实现，普通流程和旧 Quality 保留。
- [ ] 无客户端抽样/差异判断/时长累加，无读取他人稿件补字段的旁路。
- [ ] 样本领取到管理员裁定的真实端到端流程通过。
- [ ] 新增浏览器测试、受影响后端测试和既有回归有实际通过证据。
- [ ] 配置、裁定的幂等重试与过期快照冲突可复现验证。
- [ ] Unicode 差异高亮、分段音频定位和英文/阿语呈现已实看截图。
- [ ] 前端开发报告放入 `docs/plans/cross-annotation-quality-frontend-development-report.md`，列出改动文件、页面入口、实际请求、测试/截图产物、未完成事项与限制。
- [ ] 不把既有后端 10 万条容量缺口写成已验收，不覆盖本计划之外的已有工作区改动。
