# 随机交叉质检：后端与数据库实施计划

日期：2026-09-18。实施对象：grok build。目标分支：`0918`。

开发基线：`6419a6b15e3900bbb6b5930fa8879f64e8c928e9`。状态：待实施；本文是开发交接计划，不表示功能已经实现。

## 1. 交付目标与范围

在标注员点击 **Claim next task** 时，系统按配置概率，从符合该标注员当前场景选择及权限的音频中，随机领取一条**由他人完成的音频**进行独立复标。候选包含服务上线以来的历史成果及以后新增的成果。复标完成后比较两份结果，词差异率严格大于 10% 或存在约定异常时，进入管理员质检；管理员通过后端接口裁定最终结果。

本轮交付数据库迁移、后端领域逻辑、普通标注接口兼容、管理员接口、导出限制、统计去重、审计及自动化测试。**不修改 HTML、CSS、前端 JavaScript，不开发质检页面、交互或视觉设计。** 后端必须能通过 API 独立完成整个流程，不能把管理员裁定、导出限制或权限校验留给下一轮前端开发。

### 1.1 已确认的业务要求

1. 随机触发点是领取请求，不是在提交时抽样，也不预先生成一批音频再指派两名标注员。
2. 历史、新增的已标注音频使用同一候选规则，不设上线日期门槛。
3. 复标者必须是另一名标注员，且领取结果必须符合当前选择的场景及该用户的权限。
4. 有在途任务时仍强制续领；领取概率只用于没有在途任务的新领取。
5. 复标从原始预标注基线开始，不能看到原标注员的文本、Bad Quality 判断或人工场景核验。
6. 原结果、复标结果分别保存；本轮比较固定到领取时的原结果版本。
7. 词级最少增删改次数除以较长文本词数；严格 `> 10%` 进入管理员质检，恰好 10% 不因词差异触发质检。
8. 跳过、空文本、Bad Quality 判断冲突也须进入异常质检。
9. 差异率不超过 10% 且无异常时沿用原结果；否则由管理员选用原稿、复标稿或修正后发布。
10. 被领取复标后，在复标及裁定完成前暂停该音频的**后续训练导出**。不能宣称能收回已生成的数据集。
11. 两人的工作分别留档，但同一音频的语料数量、**总计标注音频时长**只能计算一次。

### 1.2 本轮明确采用的实现细则

以下细则用于消除实施歧义，不增加前端范围：

| 项目 | 首版规则 |
|---|---|
| 抽样比例 | 启用后默认 10%，可配置；与固定的 10% 词差异阈值独立 |
| 上线开关 | 新数据库配置行默认关闭新复标领取，比例保存为 10%；测试显式开启，部署验证后可通过管理员接口开启 |
| 候选成果 | 当前有效的 `annotated` 发布版本；不从 `pending`、`skipped`、已撤销版本中抽取 |
| 人员排除 | 排除本人曾领取、编辑或提交过的音频，以及现有禁止重领规则命中的音频 |
| 同时占用 | 同一音频最多一个未结束的交叉质检轮次；同一标注员最多一个在途任务 |
| 重复抽检 | 同一原发布版本最多一个未取消、未失效的轮次；该轮最终发布版本也视为已覆盖，避免裁定后立即再次复标 |
| 后续纠正 | 后续正常纠正产生新的发布版本后，可以重新成为候选；取消的轮次允许由其他未参与者重新领取 |
| 普通池为空 | 启用复标且比例大于零时，允许回退到复标池，避免有可做任务却随机返回无任务 |
| 复标池为空 | 回退到原有普通任务领取规则；不能放宽场景或人员限制来凑候选 |
| 自动通过的含义 | 仅代表两份结果达到一致性规则，不等于认定语音转写绝对正确 |

不做定时预抽样、标注员最低抽查配额、风险加权抽样、第三人仲裁、自动判责、人员质量排名、通过样本的额外人工抽查或外部通知。上述能力可在后续单独规划。

## 2. 现状与关键改动入口

当前只有 `migrations/001` 至 `007`。新增迁移，不改写已经应用过的 SQL。

| 入口 | 现状 | 必须处理的影响 |
|---|---|---|
| `annotation_repository.py:claim/get_assignment` | 一个任务一个 assignment，一个人一个 assignment；已有任务优先恢复 | 增加领取分流与复标恢复，不另建一套会绕过每人单任务限制的领取表 |
| `annotation_metadata/claiming.py`、`queries.py` | 普通领取只查询 pending/draft，按场景权限及同一来源证据匹配 | 复用来源匹配谓词，单独建立 published 候选查询；不能直接调用仅支持 pending 的完整查询 |
| `annotation_repository.py:complete` | 当前完成会直接 supersede 原版并发布工作版本 | 复标提交必须走独立分支，不能先发布第二份再比较 |
| `annotation_versions`、`segments` | 每音频一个 live draft、一个 live published，segment 随版本存储 | 复用版本和片段模型，新增复标提交生命周期及用途字段 |
| `annotation_metadata/serializers.py` | 纠正草稿会返回 `reference_review`、已发布核验及模型信息 | 增加服务端盲标序列化策略，防止复标领取泄露原结果 |
| `completed_list/detail`、`history_recent`、`authorized_media` | 主要围绕本人当前发布版本 | 复标者需要查询自己的提交和媒体，但不能借 task ID 读取另一人的结果 |
| `dashboard`、`public_annotation_speed`、Admin 统计 | 有效成果通常从 task 的当前发布版本统计；部分总计来自人员排名聚合 | 全局总时长直接按唯一 task 汇总，新增工作量不得混入语料时长 |
| `scripts/export_top_annotators_tar.py` | 有独立的排名、片段、排除统计查询及快照 | 所有训练选择与计数一致应用质检导出限制，不能只改主片段查询 |
| `export.py`、`manage_state.py`、备份模块 | Excel 统计、兼容 JSON、元数据及完整数据库备份各有用途 | 区分训练交付和审计备份，备份必须保留待质检数据 |
| 撤销、恢复、停用、释放、场景核验修正 | 可修改发布指针、草稿、assignment 或元数据 | 与交叉质检状态联动，避免孤立草稿、失效裁定或永久导出阻塞 |

建议新增 `annotation_quality/` 包：

```text
annotation_quality/
  contracts.py       # 请求模型、响应模型、状态和原因码
  repository.py      # 使用调用方 cursor 的读写，不自行提交事务
  claiming.py        # 候选条件、随机选择、领取分流
  comparison.py      # 规范化、词级距离、差异定位、质量标记冲突
  service.py         # 复标提交、自动判定、裁定、取消/失效
  serializers.py     # 标注员本人视图与管理员完整视图
  queries.py         # 共享统计和训练导出资格条件
  routes.py          # 管理员接口及本人复标记录接口
```

沿用现有 Flask、psycopg、Pydantic、数据库迁移和 Admin 审计机制。新模块接收同一事务的 cursor；不引入 Celery、Redis 或定时抽样服务。注册 Blueprint 时注入现有鉴权装饰器，避免反向导入 `server`。

## 3. 状态与不变量

### 3.1 一条音频、多个结果、一个有效发布版本

一条音频始终对应原 `annotation_tasks.id`。不能复制 task、音频路径或波形来代表第二人的工作。

```text
原已发布结果 A
  → 领取时命中随机复标：新建轮次和基线草稿 B，状态 in_progress
  → B 提交：
       差异率 <= 10% 且无异常 → passed，A 继续有效
       差异率 > 10% 或有异常 → awaiting_review，A 保留但训练导出受阻
  → 管理员裁定 → adjudicated，确定唯一最终发布版本
```

| 轮次状态 | 含义 | 是否有复标 assignment | 是否阻止训练导出 |
|---|---|---|---|
| `in_progress` | 正在复标，可保存和恢复 | 是 | 是 |
| `awaiting_review` | 第二份已提交，等待管理员裁定 | 否 | 是 |
| `passed` | 自动一致性通过 | 否 | 否 |
| `adjudicated` | 管理员已裁定 | 否 | 否；最终成果仍须符合 annotated 等训练条件 |
| `cancelled` | 未提交的复标被放弃或显式取消 | 否 | 否；原成果恢复原有资格，此状态不是质检通过 |
| `invalidated` | 原成果被撤销等操作使本轮失效 | 否 | 由新的有效成果状态决定；不能继续采用本轮旧结论 |

`awaiting_review` 不允许通过普通取消直接恢复导出。须完成裁定，或者通过显式撤销原成果使任务回到 pending；不能让关闭开关或标注员退出绕过已发现的分歧。

### 3.2 必须保持的不变量

- 继续保留 `assignments.user_id` 主键、`assignments.task_id` 唯一约束，以及每 task 一个 live draft / published 的约束。
- 普通标注、本人纠正、复标共用同一个在途任务限制。
- 复标期间 task 的 `status` 和 `current_published_version_id` 保留原成果，质检状态另存；不得用把音频改成 pending 的办法暂缓导出。
- 自动通过不改原结果正文、发布者或 `submitted_at`，不会凭空生成一次新增语料提交。
- 两份已提交的原始文本不可原地改写；管理员修正创建派生版本。
- 修改配置只影响新领取；已有复标仍可恢复、保存、提交和裁定，已有导出限制继续生效。

## 4. 数据库迁移

新增 `migrations/008_cross_annotation_quality.sql`；若实施时编号已被其他开发占用，使用下一个连续编号，并同步文档及测试。以下字段名构成推荐实现契约，SQL 类型和约束必须实际落地。

### 4.1 `cross_check_settings`：数据库单一配置源

| 字段 | 类型 / 规则 |
|---|---|
| `id` | smallint PK，CHECK = 1 |
| `enabled` | boolean NOT NULL，初始 false |
| `sampling_rate_bps` | integer，0–10000；初始 1000，即 10% |
| `revision` | integer NOT NULL，初始 0；管理员乐观锁 |
| `updated_at` | timestamptz |
| `updated_by_admin_action_id` | nullable FK → admin_actions |

本轮词差异阈值固定为 1000 basis points，不提供改变它的写接口。每个轮次保存阈值及比较算法版本，保证以后改变实现仍能解释历史结论。所有 Gunicorn worker 读同一配置，不能在进程内各自保存可变配置。

### 4.2 `cross_check_rounds`：每次复标的主记录

| 字段组 | 字段与含义 |
|---|---|
| 身份 | `id` UUID PK、`task_id` FK、`revision` integer |
| 原结果 | `original_version_id`、`original_annotator_id`、`original_review_id`（可空），均在领取时固定 |
| 复标结果 | `secondary_version_id` UNIQUE FK、`secondary_annotator_id` FK |
| 起点 | `baseline_version_id` FK、`baseline_quality` 快照 |
| 状态 | `state`，使用第 3 节枚举 |
| 抽取上下文 | `settings_revision`、`sampling_rate_bps`、`claim_policy`、`claim_filters` JSONB；场景和来源证据继续记录在 assignment/event 中 |
| 比较口径 | `comparison_version = worddiff_v1`、`threshold_bps = 1000` |
| 比较结果 | `original_word_count`、`secondary_word_count`、`edit_distance`、`substitutions`、`insertions`、`deletions`；未计算时 NULL |
| 证据 | 两份文本的规范化摘要、输入版本 revision 快照、差异操作/片段映射 JSONB、`reason_codes` text[] |
| 裁定 | `decision`（original / secondary / edited）、`final_version_id`、`decided_by_admin_action_id`、`decision_reason` |
| 时间 | `created_at`、`submitted_at`、`compared_at`、`resolved_at`、`updated_at` |
| 终止说明 | `termination_reason`，用于取消/失效；不可替代裁定记录 |

数据库约束：

1. 原标注员和复标员必须不同；两份版本必须不同。
2. 原稿、复标稿、基线和最终稿都必须属于同一 `task_id`。优先用 `(task_id, id)` 复合唯一键及复合外键保证；服务层再校验用途、生命周期和作者。
3. 对 `task_id WHERE state IN ('in_progress','awaiting_review')` 建唯一索引。
4. 对 `original_version_id WHERE state NOT IN ('cancelled','invalidated')` 建唯一索引。
5. 为 `final_version_id` 建查询索引，候选检查同时排除已通过/已裁定覆盖的最终版本。
6. 状态约束保证 passed 必有合法比较结果且无异常；awaiting_review 必有提交时间和至少一个原因；adjudicated 必有 final version、decision 和 admin action。不能把 NULL 距离当作零。
7. 对 `(state, created_at, id)`、`(secondary_annotator_id, submitted_at, id)`、`(task_id, created_at, id)` 建列表/审计索引。

### 4.3 扩展已有版本与 assignment

- `annotation_versions.purpose`：`annotation / cross_check / adjudication`，旧数据默认 annotation；普通 revision 仍属于 annotation。
- lifecycle 新增 `cross_check_submitted`。复标草稿仍为 draft；提交后进入该状态，即使自动通过，也不会被当作新的 published 成果。
- `annotation_versions.credited_annotator_id`：可空 FK，现有已提交版本回填实际提交者；用于有效成果的人员归属，不冒充实际操作人。
- `annotation_versions.published_by_admin_action_id`：可空 FK，记录管理员采用复标稿或编辑发布的操作。
- `assignments.mode` 增加 `cross_check`；新增可空 `cross_check_round_id` FK。
- CHECK 保证只有 cross_check assignment 带 round ID，且工作版本、task、用户与该轮一致；跨表一致性由同事务写入加必要的约束触发器或受控写入口保证，并写失败测试。
- 保留 `base_version_id`：复标稿指向干净 baseline；管理员编辑稿指向所选原稿或复标稿。不能把 baseline 的指针换成他人发布版本。

人员归属与实际作者必须区分：普通/复标提交的 `submitted_by_user_id` 是真实提交者；管理员编辑生成的最终版本该字段为 NULL，`credited_annotator_id` 继承选定底稿的有效归属，实际管理员保存在 action FK。有效语料排名可使用归属人；本人内容访问、参与排除、撤销权限不能仅凭归属人字段放行。

### 4.4 `task_annotation_participants`：避免自己复标自己

新增 `(task_id, user_id)` 复合主键及 `first_participated_at`。成功领取、本人 reopen、导入历史作者等入口在同事务执行幂等插入。参与后即使放弃、撤销或被替代，记录仍保留。

迁移从以下来源取并集回填：现有 assignment；版本的 created/modified/submitted 用户；历史 claimed/reopened/completed 等真实参与事件。跳过 NULL 和系统操作，不把 credited 字段自动当作实际参与证明。

迁移必须检查重复、跨 task 引用、无基线、无可识别原作者等异常。无法安全判断作者或建立基线的历史音频暂不进入候选，并在管理员统计中报告数量；不能凭用户名猜作者或使用已标注文本补成“原始预标注”。正常历史音频不得因日期早于迁移而被排除。

## 5. 领取、随机性与场景权限

### 5.1 领取顺序

```text
验证用户和 session fence，锁定当前用户
  → 有 assignment：恢复它，不抽随机数，不应用新的场景选择来换任务
  → 无 assignment：校验所选场景/批次/置信度及用户 scope
  → 优先按现有规则恢复符合筛选的 migration-reserved draft（若有）
  → 读取配置；启用且 sampling_rate_bps > 0 时，掷一次领取类型随机数
  → 命中复标：尝试随机领取复标候选；无可锁候选则回退普通池
  → 未命中：正常领取；普通池无可锁候选且允许复标时回退复标池
  → 两池均无可领：沿用 NoTaskAvailable / TaskPoolBusy 的区分
```

`sampling_rate_bps=0` 或 enabled=false 时不创建任何新轮次，即使普通池为空。10000 表示优先尝试复标，复标不可用时仍可做普通任务。10% 是两个池均可领取时的类型选择概率，不是每人每十条必有一条，也不是每天精确 10% 的配额。

候选锁竞争的内部重试不重新掷类型随机数；同一用户并发 claim 仍由用户锁收敛到同一个 assignment。网络重试在 assignment 已创建时恢复原结果，不再新建轮次。

### 5.2 复标候选必须同时满足

- 当前 task `eligible=true`、`status='annotated'`，当前版本 lifecycle=published、target_status=annotated，原提交者可识别且符合现有账户状态规则。
- 没有 assignment、live draft、未结束轮次、有效预处理占用或冲突保留任务。
- 请求用户不在该 task 的历史参与者集合内，不命中 `task_annotator_blocks`。
- 当前发布版本没有被已完成轮次作为 original 或 final 覆盖。
- 存在可用 baseline。缺失基线的单条数据不能导致整次 claim 500。
- 同时满足 `source_scene`、scope、`batch_code`、`source_confidence`；复用 `claim_source_exists_sql` 的同一来源证据语义。
- 多来源不能放大抽中概率；候选按 task 去重。Spoken languages / 无来源 / NULL 场景继续使用现有统一分类，不另造 unknown 场景。

普通领取继续使用现有优先级。复标候选在匹配集合中随机挑选，不再额外按高置信度优先，以免所有复标都集中到同一类来源。

### 5.3 随机选择与性能

首版可采用“过滤候选计数 → 随机序号 → 无锁取候选 ID → 按该 ID 锁定 task 并重验”的实现：

1. 所有筛选查询使用共享候选谓词，稳定排序为 allocation_order、task ID；随机整数由服务端生成，测试可注入随机源。
2. 无竞争、候选集合稳定时，各候选抽取概率相同。并发变化时重验并有界重试，不能承诺锁竞争下的严格均匀。
3. 只锁最终候选；不要把 `OFFSET` 直接放进带 `FOR UPDATE` 的大范围候选查询，因为跳过的行也会被锁。[PostgreSQL SELECT 锁定说明](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE)
4. 每次复标尝试最多 8 次候选锁定/重验，之后尝试另一池；两池有候选但均不可锁时返回暂时忙，不报无数据。
5. 不把整个音频池搬进 Python，不在每次普通领取上执行全量随机排序。随机序号方案仍可能扫描较多候选，必须完成第 13 节容量验收；若优化选择算法，须保留可解释的抽样行为及同等资格约束。

### 5.4 创建复标任务的原子边界

锁定 task 后重新检查发布指针、候选资格和场景来源，再在同一事务内：

1. 固定原版本及其人工核验版本，记录配置和筛选快照。
2. 从 baseline 创建新的 cross_check draft，保留 ASR/原始分段，不复制原人的 text、Bad Quality 或人工核验。
3. 创建 `in_progress` 轮次及 cross_check assignment，分配 lease token。
4. 写入参与者记录和 `cross_check_claimed` 事件。
5. 提交后导出限制立即生效；任何一步失败全部回滚，不留孤立轮次或草稿。

## 6. 盲标、保存与比较

### 6.1 服务端控制可见内容

复标 assignment 返回自己的工作版本、音频、波形、原始 ASR、允许的来源信息及本次领取场景。原稿全文、原作者、原稿 Bad Quality、原稿核验、差异率和裁定内容仅由管理员接口返回。

必须逐一检查以下泄露路径：

- `get_assignment` 与 claim 的初次响应必须应用同一盲标策略；刷新、接管、重新登录不能绕过。
- `metadata.reference_review` 固定为 NULL，不将 published review 回退为当前工作核验；自己的 draft review 正常可读写。
- 隐藏基于原人工稿产生的模型预测；首版复标视图将 `metadata.prediction` 置 NULL，来源场景照常展示。
- `headline` 仅由安全字段重新生成，不能包含原稿人工核验的结论或备注。
- 不把原版本或 baseline 的 `extra` 整包透传到复标草稿。复制已知技术字段的白名单，并防止 `_load_segments()` 中的 extra 覆盖 text、ASR、时间或身份字段。
- 历史 reconstructed baseline 使用已有原始 ASR 和可恢复的分段，text 保持空、Bad Quality 重置；记录 `baseline_quality`，不能伪称为完全原始数据。
- 复标文本详情按 version 与实际提交者鉴权，不能仅凭知道 task/round/version UUID 就看到他人结果。

现有 UI 是否展示“正在复标”留给后续产品设计。首版接口明确返回真实 `mode='cross_check'`，不能伪装成普通任务来规避协议适配；新领取默认关闭使既有线上页面保持原有流程。

### 6.2 自动保存和提交

保存继续使用现有 lease token、session fence、expected revision、operation ID 和 request hash。自己的草稿 revision 每次保存递增；任何写入都必须验证 round 仍为 in_progress，且 assignment 属于该用户及该轮次。

提交比较的输入是**数据库已保存的完整草稿合并本次请求的 dirty segments**，不能只比较请求里出现的片段，也不能忽略尚未自动保存的最后修改。

复标完成采用以下步骤，避免把较长文本计算放在持锁事务中：

1. 鉴权后读取固定原版本和完整复标草稿快照，合并本次修改，在内存中执行与正常保存一致的字段、片段、时间范围验证。
2. 在不持有用户/task 写锁的情况下执行纯函数比较，记录输入摘要及草稿 revision。
3. 进入写事务，按第 10 节锁顺序检查 fence、幂等记录、assignment、task、round 和版本；再次验证 expected revision、原发布指针、两份比较输入摘要。变化时返回 409，不写入过期比较。
4. 写最后修改和自己的场景核验，将复标版本冻结为 cross_check_submitted，记录真实提交者与提交时间，保存比较证据并转为 passed 或 awaiting_review。
5. 删除 assignment，写入幂等结果及 `cross_check_submitted` 事件，原子提交。

对 operation ID 的重放要在耗时比较前做一次只读快速检查，并在最终事务内再次检查。相同请求不会再次产生版本、轮次、统计或质检记录；不同内容复用 operation ID 返回冲突。

普通 annotated 提交的“非 Bad Quality 片段不可为空”校验保持现状。复标的非法格式/时间同样直接拒绝；合法地全部标为 Bad Quality、规范化后无词，或提交 skipped 时，应保存该事实并转人工质检。不能为了处理异常质检而放开普通完成接口的有效性校验。

### 6.3 `worddiff_v1` 的确定性定义

1. 对双方各自的片段按 `(start_s, end_s, segment_id)` 排序，仅拼接未标记 Bad Quality 的文本。片段之间加入空格，不要求两人切分边界一致。
2. 文本先做 Unicode NFC，再 casefold，再规范化标点和空白。
3. 词内连接字母的撇号/连字符删除，其余 Unicode 标点转换为空格；连续空白合并，去掉首尾空白。固定这份规则及字符识别实现并写示例测试，例如 `Hello!` 与 `hello`、`can't` 与 `cant`。
4. 按规范化后的空白分词。首版适用于当前按空格书写的阿拉伯语/英语数据；不引入语义相似度、词干化、同义词合并或数字读法转换，不擅自合并阿语不同字母及去掉变音符号。
5. 使用单位代价的词级 Levenshtein 距离，替换/插入/删除代价均为 1。不能用 `difflib.SequenceMatcher.ratio()`、字符差异率或有“标准答案”方向性的指标替代。
6. 保存逐词匹配/替换/插入/删除操作，以及它们到双方原片段、原文本区间和音频时间区间的映射，为下一轮前端高亮提供数据。时间精度来自所属片段，不伪造逐词强制对齐时间。等成本路径采用固定优先级，保证结果可重现。

令 `D` 为编辑距离，`N=max(N_original,N_secondary)`，阈值 `T=1000`：

```text
word_difference_rate = D / N                 # 仅 N > 0 且两侧非空时给正常比率
needs_word_review = D * 10000 > N * T         # 整数比较，禁止四舍五入后判断
```

若任一侧规范化词数为零，正常比率返回 NULL，并记录 empty 原因进入人工质检；一侧非空时可以保存实际 D，但不能因空文本而自动通过。100 个词差 10 个是 10%，差 11 个是 11%；10 个词差 1 个不因词差异入队，9 个词差 1 个入队。

原始文本和 ASR 不做覆盖性规范化。距离和统计都是附加证据，不改变用户提交的文本。

### 6.4 异常原因码及资源边界

| 原因码 | 触发条件 |
|---|---|
| `word_difference_exceeded` | 整数阈值比较严格超过 10% |
| `submission_status_conflict` | 原稿 annotated，复标提交 skipped |
| `empty_original_text` | 原稿可用文本规范化后为空，包括历史异常数据 |
| `empty_secondary_text` | 复标可用文本规范化后为空，包括全 Bad Quality |
| `bad_quality_conflict` | 双方排除的音频时间范围不一致 |
| `comparison_unavailable` | 可恢复的计算故障或输入规模超过计算预算，保留子原因 |

Bad Quality 按排除片段的时间区间并集比较，不能按“第几个 segment”或 Bad Quality 片段数量比较。时间先量化为毫秒，采用与现有时间校验相容的 1 ms 数值容差；纯片段拆分而覆盖范围相同不算冲突。两侧均无 Bad Quality 时，普通切分边界差异不增加该原因。

比较函数必须有有限内存与计算预算。首版设置 `MAX_COMPARISON_CELLS=10_000_000`（两侧词数乘积），超过后不执行无界二维 DP，转人工质检并记录 `comparison_unavailable/input_too_large`。使用该上限时仍须通过长文本性能测试；在预算内返回精确结果，不能把截断后的近似比率当成通过依据。

对可恢复的比较异常可以保存有效提交并转人工；数据库错误、权限错误和提交验证错误仍应失败回滚，不得被通用 try/except 吞成“质检成功”。管理员详情须明确指出没有自动差异结果，仍能查看两份完整文本并裁定。首版不依赖后台比较 worker。

## 7. 后端接口契约

所有新请求使用严格模型，拒绝未知字段，整数字段拒绝 boolean 及非整数。沿用现有 RepositoryError、登录/session fence、Admin Session、CSRF、请求审计与 operation ID 机制。UUID、日期、分页游标必须校验。

### 7.1 现有标注接口的扩展

| 接口 | 约定 |
|---|---|
| `POST /api/assignment/claim` | 请求仍仅接受原有场景/批次/置信度筛选；客户端不能传概率、强制复标或指定他人结果 |
| `GET /api/assignment` | 只读恢复，同一复标 assignment 不重新随机选择 |
| `PATCH/POST /api/assignment/current` | 保存自己的复标草稿，沿用 revision 与幂等契约 |
| `POST /api/assignment/current/complete` | 以数据库中的 mode 决定提交分支；客户端不能通过改 mode 让复标稿直接发布 |
| `POST /api/assignment/current/abandon` | 普通任务行为保留；复标草稿转 abandoned，轮次 cancelled，释放 assignment，原发布成果不变 |
| `GET /api/audio/<task_id>`、`/api/waveform/<task_id>` | 在途复标者可访问；提交后的本人复标记录也支持相应媒体授权 |

复标 assignment 的关键响应字段示例（原有文件、segments、波形、metadata 字段仍在）：

```json
{
  "assigned": true,
  "mode": "cross_check",
  "task_id": "11111111-1111-4111-8111-111111111111",
  "version_id": "22222222-2222-4222-8222-222222222222",
  "revision": 0,
  "status": "annotated",
  "resumed": false,
  "cross_check": {
    "round_id": "33333333-3333-4333-8333-333333333333",
    "state": "in_progress"
  }
}
```

这里 status 是音频原有状态，version_id 是本人可编辑的独立草稿；完整响应仍须带 lease_token。不得向复标者返回 original_version_id、原作者、原稿内容或比较结果。

复标提交的关键响应字段示例：

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

提交响应的 status 表示这次本人提交的 annotated/skipped 结果，不表示复标稿已经成为音频当前版本。正常标注响应继续保持原契约；新字段只能增量添加。错误状态和成功重放结构需与 `api_complete()` 现有包装逻辑统一。

### 7.2 配置接口

`GET /api/admin/cross-check-settings` 返回当前配置、revision、只读 `word_difference_threshold_bps=1000` 和 comparison_version。

`PUT /api/admin/cross-check-settings` 请求示例：

```json
{
  "operation_id": "44444444-4444-4444-8444-444444444444",
  "expected_revision": 0,
  "enabled": true,
  "sampling_rate_bps": 1000,
  "reason": "Enable cross-check claims at 10 percent"
}
```

成功返回新配置、递增 revision 及 action_id；旧 revision 返回 409。相同操作重放返回相同结果，不能递增两次。关闭新领取后，已有未完成轮次及导出限制继续生效。配置变更写入 `admin_actions`，不在普通标注响应中暴露管理信息。

### 7.3 管理员队列和详情

- `GET /api/admin/cross-checks`：默认 state=awaiting_review，支持指定状态、source_scene、batch_code、original_annotator_id、secondary_annotator_id、reason_code、q、from/to、limit、cursor。日期使用现有管理员时区及半开区间规则，过滤本轮 created_at；响应返回 applied_filters。每页默认 50、最大 100，游标绑定筛选条件并按 `(created_at,id)` 稳定排序。
- 列表返回 round/task 身份、两名人员、轮次状态、时长、词数、差异率或 NULL、原因码、创建/提交/裁定时间、是否阻止导出；列表不返回全文。
- `GET /api/admin/cross-checks/<round_id>`：返回完整两份原始片段、各自核验快照、指标、逐词差异及时间映射、配置/版本快照、round revision、当前有效版本、裁定记录和管理员音频地址。比较不可用时显式返回原因。
- 扩展 `GET /api/admin/quality` 的返回数据，新增独立 `cross_check` 汇总及待质检入口信息；保留现有 fast/stale/bad quality 信号字段及含义。完整分页队列以新接口为准，不强塞进旧 limit=50 的质量列表。

普通标注员访问以上接口一律拒绝；不能通过隐藏字段而仍允许读管理员详情。

### 7.4 管理员裁定

`POST /api/admin/cross-checks/<round_id>/decision` 使用 Admin 写鉴权及 CSRF。

```json
{
  "operation_id": "55555555-5555-4555-8555-555555555555",
  "expected_revision": 1,
  "expected_original_version_id": "66666666-6666-4666-8666-666666666666",
  "expected_secondary_version_id": "22222222-2222-4222-8222-222222222222",
  "decision": "original",
  "reason": "Checked the recording and accepted the original transcript"
}
```

三种 decision：

| decision | 行为 |
|---|---|
| `original` | 沿用原发布版本，不改正文/归属/提交时间；关闭轮次并解除限制 |
| `secondary` | 将冻结的复标稿发布为唯一当前版本，原稿 superseded；保持复标稿的原始提交内容与真实提交者，记录管理员发布 action |
| `edited` | 必填 `base=original/secondary`、完整 `segments`、`target_status`、`skip_reasons`；可带 `scene_review`；从所选底稿建立新 adjudication 版本并发布 |

edited 继续遵守现有 segment ID、非重叠、音频范围、非空有效文本等校验，不扩展为任意新增/删除分段编辑器。采用 skipped 结果时必须有现有合法 skip reason，最终 task 为 skipped，因此不能进入训练导出。original/secondary 请求不得夹带 segments 等编辑字段。

复制/编辑场景核验必须建立正确的版本关联，原始核验历史不被改写。edited 的真实操作者为管理员，人员归属按第 4.3 节处理。两份比较原稿仍可完整读取，比较指标不因管理员修正文案而被覆盖成零。

事务内要求 round=awaiting_review、expected revision/versions 全部匹配、task 当前发布指针仍为原版本、没有新 assignment/draft。裁定、发布/保留、导出解除、审计和幂等结果同事务完成。返回 success、action_id、round_id、state、final_version_id、final_status、training_export_blocked。

两个管理员同时裁定只能有一个成功。已结束轮次只允许原 operation ID 的幂等重放；新 operation ID 返回 409，不能悄悄改判。

### 7.5 取消与本人记录

- `POST /api/admin/cross-checks/<round_id>/cancel`：operation_id、expected_revision、reason、confirm=true；只允许 in_progress。释放 assignment、废弃草稿、轮次 cancelled；保留参与记录及原成果。
- awaiting_review 不能调用 cancel；若原成果确实应作废，走已有管理员撤销流程并同步 invalidated。
- `GET /api/cross-checks/mine`：返回当前用户已提交的复标记录，按 `(submitted_at,id)` 游标分页，不依赖该稿是否最终被采用。
- `GET /api/cross-checks/<round_id>/submission`：仅该轮实际复标提交者可读自己的冻结结果及自身核验，不含原稿、对方身份、差异明细或管理员编辑稿。未提交时使用现有 assignment 接口。
- 保持现有 `/api/completed` 和 history 的普通成果语义，不把未发布复标伪装成普通 published 成果。本人复标历史由新接口提供，为后续 UI 接入准备。
- 媒体访问可基于本人真实提交证明扩展；“有权听音频”不等于“有权读取该音频所有版本”。归属字段不能替代内容访问鉴权。

统一错误：未登录 401、无权限/CSRF 403、资源不存在 404、旧 revision/状态/版本冲突 409、非法字段 400。新增稳定错误码包括 `cross_check_active`、`cross_check_state_conflict`、`cross_check_version_changed`；保留现有 session changed、revision conflict 语义。

### 7.6 管理员编辑成果的后续撤销/恢复

当前 revoke/restore 假设发布版本一定有普通 submitter。增加管理员 edited 版本后，必须补齐此路径，不能创建无法管理的成果：

- 既有 `/api/admin/annotations/revoke/preview` 和 `/revoke` 增量允许 `annotator_id=null`，但仅限显式 items 列出 task_id + expected_version_id，且所有目标确认为 `purpose=adjudication`、实际 submitter=NULL、存在 published_by_admin_action_id 的版本；不能把缺失字段或普通历史作者缺失静默当成此模式。
- 该模式必须 `block_reclaim=false`，不创建 user_id=NULL 的禁止重领记录。其他确认、批量二次密钥、revision/版本检查和原子性保持现状。原有非空 annotator_id 的请求行为不变。
- revoke 审计 item 的 annotator_id 允许 NULL，details 明确实际作者为 admin，并保存来源发布 action；restore 通过该身份和审计链校验，不再要求一个不存在的普通用户处于 active 状态。
- 普通版本恢复仍检查真实原提交者状态；管理员版恢复仍检查原撤销 action、干净草稿、没有被重领/修改等现有条件。
- 人员停用不因 credited 归属自动撤销管理员已编辑确认的结果。实际作者、人员归属和管理员审计在响应中明确区分。

以上是现有后端管理接口的兼容扩展，不增加前端页面。本人的 reopen 仍按真实提交者授权，不能通过 credited 字段获得管理员编辑稿的写权限。

## 8. 统计：语料总时长与人员工作量分开

### 8.1 全局总量的唯一口径

全局已标注条数和总计标注音频时长，以 `annotation_tasks.id` 为唯一语料单位。只对当前有效 published/annotated 成果计入 `annotation_tasks.duration` 一次，不遍历所有提交、版本、人员记录或质检轮次相加。

核心汇总应等价于：

```sql
SELECT count(*) AS annotated_count,
       COALESCE(sum(t.duration), 0) AS annotated_duration_seconds
FROM annotation_tasks t
JOIN annotation_versions v ON v.id = t.current_published_version_id
WHERE t.status = 'annotated'
  AND v.lifecycle = 'published'
  AND v.target_status = 'annotated';
```

额外 join 场景、轮次等一对多表时必须先得到唯一 task 集合或使用 EXISTS。**禁止 `SUM(DISTINCT duration)`**：两条不同音频都为 60 秒，应合计 120 秒，不能因时长相同合并。

待复标/待裁定仅降低“可训练导出时长”，不会使原本已经标注的总时长消失；最终改为 skipped 或撤销时，才按现有有效成果规则从 annotated 总时长中移除。

| 60 秒音频的阶段 | 已标注语料总时长 | 本轮复标已提交工作量 | 可训练导出音频时长 |
|---|---:|---:|---:|
| A 已完成 | 60 | 0 | 60 |
| B 正在复标 | 60 | 0 | 0 |
| B 已提交、待裁定 | 60 | 60 | 0 |
| 自动通过或裁定 annotated | 60 | 60 | 60 |
| 裁定 skipped | 0 | 60 | 0 |

这里的“可训练导出音频时长”是符合条件的完整音频时长，若原接口统计 usable segment 时长，则继续按最终版本非 Bad Quality 片段单独求和，不能混用两个量纲。

### 8.2 各接口与查询的调整

- `dashboard().stats.annotated_duration_seconds` 改为直接从唯一当前有效 task 汇总，不能以可能包含两份工作量的排行榜合计作为来源，也不能因管理员发布版本没有普通 submitter 而漏计。
- 现有 leaderboard 的 duration/hours 继续表示当前有效成果的唯一归属；使用统一的 `COALESCE(credited_annotator_id, submitted_by_user_id)` 归属规则。复标稿未采用时不增加该排行榜的有效语料时长。
- Admin overview、annotator detail、annotations、tasks 等按相同的当前成果规则计数；统计筛选里的人员归属与“实际作者/审计操作者”要分字段，禁止互相借用鉴权逻辑。
- 新增独立复标工作量字段，例如 `cross_check_submitted_count`、`cross_check_submitted_audio_seconds`。两人的工作分别存在记录中，但不能把这类字段加回语料总时长。
- 新增 `cross_check.in_progress_count / pending_review_count / blocked_audio_seconds / passed_count / adjudicated_count` 等管理汇总；计数分别标明是轮次还是唯一音频。
- 普通 pending 队列和 assigned 统计要排除已标注音频上的复标 assignment，再单独返回复标在途数量，避免 pending-assigned 出现负数或误报普通工作积压。
- 当前用户 pool 增加 `normal_available`、`cross_check_available`。原 `available` 为当前配置下可实际领取的两池总数；by_scene 与 reason 使用同一资格规则。开关关闭/比例为零时复标可领数量为零；不向某用户暴露无权限场景的数量。

### 8.3 时间序列和归属变化

保留已有公开 28 日速度图“当前有效版本按 submitted_at 分桶”的语义。自动通过不改原 submitted_at，复标提交也不新增普通 completed 事件，因此不会把历史音频再次加到今日。

管理员采用复标稿时，当前有效版本变为 B，按 B 的真实提交时间归属；管理员编辑产生的新版本记录真实裁定/发布时间。依照现有纠正规则，日序列可能重新归属，但同一音频在当前快照中只出现一次。若未来要改为“首次完成日”统计，另开需求，不在本轮悄悄改变整个历史图口径。

采用复标稿导致有效成果从 A 归属到 B 时，A、B 的实际工作记录都保留，全局语料总量不增加。管理员 edited 结果保留底稿归属与实际管理员操作，两者在管理员 API 明确区分。

## 9. 训练导出、审计导出与备份

### 9.1 共享训练资格条件

在 `annotation_quality/queries.py` 提供唯一的训练导出条件构造器：

```text
当前 task 为 annotated
AND 当前版本为 published / annotated
AND 不存在该 task 的 in_progress 或 awaiting_review 轮次
AND 满足该导出原有的来源筛选、片段质量等条件
```

以 round 的状态作为阻塞真源，不额外维护容易漂移的 `blocked=true/false` 布尔字段。不能只排除 `word_difference_rate > 0.1`，因为尚未提交及计算异常也必须阻塞。

必须改动所有实际训练出口，至少包括 `scripts/export_top_annotators_tar.py`：

- 排名资格、小时门槛、片段选取、Bad Quality 排除数、异常速度排除数及 metadata 统计使用同一过滤语义。
- 同一 task 只选择 current_published_version_id；不能把 cross_check_submitted 作为第二份训练样本一起输出。
- 正确处理管理员 edited 版本及 credited 人员；避免 INNER JOIN 真实 submitter 造成管理员成果丢失。
- 时长/速度来源绑定实际内容底稿的对应 claim/submission 事件。支持 cross_check_claimed / cross_check_submitted，不能把等待管理员的天数算成标注工作时长，也不能借同一人另一次 claim 的时间。
- 在现有导出元信息/旁文件中记录 snapshot 时间、被质检阻塞而排除的唯一音频数/时长，以及已导出音频的最终版本及质检 round/outcome。保持现有 `data.json` 必需字段兼容。

数据库选取使用同一个 REPEATABLE READ 快照。以导出开始建立的快照为准：快照之后新发生的领取不会追溯重写该次导出；后续建立的新快照必须看到阻塞。音频写文件期间不长期占用 task 行锁。

### 9.2 非训练出口不丢审计数据

- `export.py` 的 Excel 是当前成果统计快照：仍按音频一行，保留待质检成果，追加清晰的质检状态/训练资格列，不混入第二人的独立工作量。
- `manage_state.py export-json` 是兼容/审计快照：保留当前成果，不能用训练过滤删除待质检音频；增量记录质检状态与训练资格。它不是全量质检恢复格式，也不能宣传为仅含训练就绪数据。
- `metadata.v1.json` 继续承担现有场景元数据范围，不将 metadata import 擅自扩展为覆盖 annotation 文本、assignment 或质检业务状态。新增版本 lifecycle/管理员核验必须能被现有导出校验处理；回归现有 exact-ID 和显式 mapping 契约。若新增事件带 round 引用，明确其属于数据库业务审计，避免作为可独立恢复的场景记录导出悬挂引用。
- 完整质检状态的恢复以 PostgreSQL custom dump/restore 为准，包含新表、两份文本、派生最终稿、当前 assignment、配置和审计。备份不能应用训练过滤。
- `annotation_metadata/postgres_backup.py` 的校验清单及 restore 演练增加新表、约束和状态关系验证。

## 10. 并发、幂等与现有管理操作

### 10.1 锁顺序及事务要求

延续现有 `complete()` 的用户 → assignment → task 顺序，不在新逻辑里先锁 task 再等待同一 assignment 持有者。建议统一受影响路径：

```text
管理员请求：operation advisory lock → admin session（现有机制）
共同业务锁：相关用户（稳定 ID 顺序）
          → 已存在的 assignments（稳定顺序）
          → tasks（稳定 ID 顺序）
          → cross_check_rounds
          → annotation_versions（稳定顺序）
```

普通 claim 只锁请求用户，随后锁候选 task；新插入的 assignment/round/版本不产生“先锁其他在途用户”的反向锁依赖。管理员要取消在途复标时，先无锁读 owner 提示，再依次锁用户和 assignment，锁后重验 owner/round。

对同时涉及多用户或批量 task 的操作，先收集目标并按稳定顺序锁定；无法在既有授权语义下安全释放他人任务时，明确返回 409 并整体回滚，不能部分修改。不要在持有 task 锁后再补锁未知的另一名用户。

所有写入在同一数据库事务使用同一连接。SQL 唯一约束是最终防线；唯一冲突转换为可重试冲突，不向客户端泄漏 IntegrityError。并发计算后的结果只能在 revision、round 状态、原发布版本及摘要均匹配时提交。

### 10.2 既有操作联动表

| 操作 | 复标在途或待裁定时的行为 |
|---|---|
| 刷新、logout、session 到期、接管、重启 | 保留原 assignment/round，按现有 fence 恢复；旧会话不能继续写 |
| 用户放弃复标 | 同事务取消 in_progress 轮次、废弃草稿、释放 assignment；原稿不变，参与记录保留 |
| 管理员释放在途 assignment | 识别 cross_check mode，执行相同取消联动；不能只删 assignment 后留下永久阻塞的 round |
| CLI `assignment-release` | 复用一致的数据库状态迁移；不能继续用仅删除/重建普通 draft 的直写逻辑绕过 round |
| 原作者 reopen | 只要存在开放轮次即返回 `cross_check_active` 409，包括已无 assignment 的 awaiting_review |
| 原版本场景核验修改 | 开放轮次期间拒绝会改变固定人工证据的修正，先完成/处理本轮；来源记录的独立更新按现有机制保留 |
| 撤销原发布成果 | 按现有确认和 release_conflicts 语义处理。允许撤销时，同事务终止相关开放轮次并释放相应草稿/assignment，然后执行正常撤销与干净 draft 重建 |
| 不允许释放冲突的撤销/恢复 | 发现开放轮次返回 409，不能只检查是否存在 assignment 而漏掉 awaiting_review |
| 停用复标者 | 其在途轮次按取消处理；已经冻结的提交证据保留，不自动选用对方稿。既有待裁定记录仍须管理员处理 |
| 停用原作者 | 按原有当前成果撤销规则执行；成果被撤销时依赖它的开放轮次同步失效。遇现有“不释放他人在途任务”限制则整体 409，不部分停用 |
| 关闭复标领取 | 仅停止新轮次；旧轮次继续恢复/提交/裁定，不能解除未完成质检的训练限制 |
| 更改场景范围 | 按现有约定不抢走已经领取的工作；只影响下一次新领取 |
| 源 metadata 更新 | 保留实际领取时来源证据快照；新领取使用最新来源。不能把旧 claim 的场景偷偷改成后来来源 |
| 重新预处理、导入覆盖 | 继续保护所有人工/发布数据，扩展保护复标草稿和已提交副本；不能删除轮次关联版本或替换其 baseline |

撤销/恢复/停用/释放的 preview、审计 summary 和失败原因须反映将受影响的轮次。不要因引入新模式降低原有 Admin 二次密钥确认要求，也不要把普通单条裁定升级成不必要的二次登录流程。

### 10.3 审计事件与可追溯性

至少记录以下业务事件：`cross_check_claimed`、`cross_check_submitted`、`cross_check_passed`、`cross_check_adjudicated`、`cross_check_cancelled`、`cross_check_invalidated`；Admin action 类型至少增加配置更新、裁定和取消。

事件记录 round/task/相关版本、操作者、状态前后、原因、算法/配置版本以及实际发生时间。真实用户提交只占用其一个 operation ID 主事件；自动判定等伴随事件不得重复使用 `annotation_events.operation_id` 的唯一值，可通过 round ID 及父事件关联。管理员裁定使用 admin_actions 的幂等机制。

复标提交不要伪造普通 `completed` 事件，否则原有新增产出统计、历史列表与质量规则会把它当成额外语料。新工作量统计明确识别 cross_check_submitted。

## 11. 迁移、上线与兼容回退

1. 在 `0918` 最新代码上确认迁移编号、工作区状态和现有约束，保留 001–007 原文件不变。
2. 在隔离数据库应用新迁移，回填人员归属与参与记录。对已有 pending、assigned、published、revoked、revision draft 和 reconstructed baseline 数据都验证迁移前后不丢内容、不改变发布指针。
3. 为新导入的历史成果、普通领取、纠正及提交补齐归属和参与记录写入，不能只完成一次迁移回填。
4. 新配置行默认 enabled=false、sampling_rate_bps=1000。新版本启动执行 schema 检查，不自动迁移；更新 health 及硬编码 schema 版本的测试/说明。
5. 部署新后端和支持新生命周期的导出/管理命令，即使关闭新领取也必须识别已有复标数据。
6. 通过接口在隔离环境开启并完成历史音频领取、自动通过、管理员裁定、训练导出阻塞与解锁的演练，再按实际部署安排修改生产配置；本计划本身不操作线上服务或生产数据库。
7. 关闭开关作为兼容回退，保留数据库结构、已存在轮次、裁定接口和导出限制。不能直接运行不认识 008/schema 与 cross_check mode 的旧二进制，也不能删除表作为回退。
8. 待质检积压通过新接口查询和人工处理；不按超时自动通过或释放。首版不引入后台通知/自动清队列。

本轮默认关闭新领取是部署兼容措施，不是功能未完成。所有业务闭环必须在开启配置的 API 自动化验收中实际运行；后续前端开发只接入已交付接口。

## 12. 实施阶段与文件交付

| 阶段 | 工作内容 | 完成标准 |
|---|---|---|
| P0：模型与迁移 | 008、新表/字段/约束、历史参与回填、配置模型、状态/原因枚举 | 空库和含历史数据的库均可迁移；重跑无副作用；原约束保留 |
| P1：比较内核 | worddiff_v1、规范化、精确距离、Bad Quality 区间比较、词与片段映射、预算异常 | 独立纯函数测试覆盖阈值、语言、切分和异常；无数据库/UI 依赖 |
| P2：领取和盲标 | 新池资格、随机分流、候选重验、baseline 副本、scope 与 metadata 隔离 | 历史成果可被领取；双方不同；scene 严格匹配；并发只产生一个 assignment/开放轮次 |
| P3：保存与提交 | 复用 session/revision/幂等，完整草稿比较、自动通过/入队、恢复与放弃 | API 完整跑通，复标不抢占原发布版本、不增加语料总时长 |
| P4：管理员接口 | 配置、列表、详情、原稿/复标稿/编辑裁定、取消、质量汇总、审计 | Admin 鉴权/CSRF、乐观锁和操作重放通过；全过程无需 UI |
| P5：现有功能联动 | reopen、撤销/恢复/停用/释放、媒体、本人的复标历史、导入/预处理保护 | 状态迁移无孤立数据，旧流程回归通过，跨版本访问不泄露 |
| P6：统计与导出 | 语料去重、复标工作量、人员归属、pool、训练资格、Excel/JSON/备份 | 同音频只记一次；待质检阻塞训练；裁定后只导出最终版本；数据库完整恢复 |
| P7：验收与交接 | PostgreSQL 16/18、既有浏览器回归、100k 容量、API 示例、开发报告 | 给出实际结果、命令与产物，前端源文件无业务改动 |

主要预计修改位置：

- 新增：`annotation_quality/`、迁移 SQL、下节所列后端测试。
- 既有后端：`annotation_repository.py`、`server.py`、`annotation_metadata/claiming.py`、`queries.py`、`serializers.py`、`reviews.py` 及必要的 contracts/导入保护入口。
- 导出/运维：`export.py`、`scripts/export_top_annotators_tar.py`、`manage_state.py`、`annotation_metadata/export_metadata.py`、`postgres_backup.py`。
- 说明：README/DEPLOY 中补充配置、schema 和回退规则；不改成另一套部署体系。
- 测试与交付证据：`tests/`、容量脚本、`docs/plans/cross-annotation-quality-backend-development-report.md` 及专用容量产物目录。

不要为新包整体重写 4900 行 repository。原入口保留为兼容外观，新增业务下沉到小模块；共享查询只抽取本轮实际需要复用的部分。实现中若必须修改上述范围外的后端入口，以依赖证据解释并覆盖测试；不扩大为前端改版。

## 13. 自动化验收矩阵

新增测试建议按 `test_cross_check_*.py` 组织。使用仓库现有隔离 PostgreSQL、Flask client、SessionFence、Admin fixture 和并发测试机制，不能用 SQLite 或全量 mock 代替事务/约束验收。

### 13.1 迁移和历史兼容

- 从 007 的含数据数据库升级，覆盖历史 published/annotated、skipped、revoked、普通在途、本人纠正、baseline=exact/reconstructed。
- 历史原作者/参与者正确回填；同一个用户无法领取自己曾经参与但已被替代/撤销/放弃的音频。
- 升级前后的 task 数、音频总时长、当前发布指针及原文本不变。
- 无 baseline/无原作者的异常记录被排除并可统计，不影响其他候选，不导致接口 500。
- 直接尝试同音频两个开放轮次、同人两份 assignment、跨音频版本引用或相同两名人员，数据库/事务拒绝并全量回滚。
- 新导入的历史作者也加入参与者集合，而非仅迁移时回填。

### 13.2 领取与随机分流

- 配置关闭、比例 0、10%、100% 均覆盖；CI 使用可注入随机源稳定覆盖命中和未命中，不能靠碰运气通过测试。
- 领取历史音频和当天新完成音频均成功，候选不限制日期。
- 有 assignment 时不抽新任务，即使请求不同场景或配置已关闭。
- 复标无候选回退普通池，普通池为空但复标可领时成功；两个池都空和锁忙的错误不同。
- `airport` 选择不能领到仅 hotel 的音频；受限 scope、无权限 scope、组合 batch/confidence、unknown→Spoken languages 均覆盖。
- 同一任务的多条来源不会增加抽样权重；“airport high”和“hotel low”不能拼成“airport low”匹配。
- 候选集合头部、中部、尾部均能被选择，防止实现实际上永远取第一条。
- 用户已有 migration-reserved 工作时保持既有优先级和筛选语义。
- 原稿版本已检查/最终版本已覆盖时不重复抽取；后续正常纠正的新发布版本可重新进入候选。

### 13.3 盲标与媒体权限

- 首次领取、GET assignment、重新登录、session takeover 均不返回原稿内容/作者/核验/reference_review/派生模型结论。
- 在 `extra` 中植入原文本、身份或保留字段覆盖值，验证副本和响应仍不泄露。
- 第二人只能编辑自己的版本，不能通过传任意 version/round/mode 改原稿。
- 原作者不能读第二份草稿；第三人不能读任一方的私有详情。
- 本人已提交的复标在未采用时仍能通过新接口读取自身结果并播放音频；媒体授权不会放开别人的文本版本。
- 管理员完整详情的权限与普通标注会话严格隔离。

### 13.4 比较正确性

- 完全一致、单词替换/插入/删除、重复词、多条等成本对齐路径。
- 差异率为 0、恰好 10%、略高于 10%；整数判定与未舍入比率一致。
- 大小写、阿语/英语标点、词内撇号/连字符、换行/多空格、Unicode 组合字符。
- 不同切分但同样有效全文得到相同比率；输出仍能映射回双方各自片段及时间。
- 数字/不同词义/阿语不同字母不会被过度规范化为相同词。
- 一侧空、双方空、全 Bad Quality、复标 skipped 分别产生明确异常。
- 相同 Bad Quality 覆盖范围但切分不同不误报；排除范围实际不同即入队。
- 保存过的文本与本次 dirty segments 合并后再比较，避免丢掉最后一次修改。
- 超过预算或可恢复比较失败：有效提交保留、转人工、距离不伪造为零、仍阻止训练导出。

### 13.5 提交、裁定及幂等

- 自动通过仍保留 A 的发布指针和 submitted_at，B 为独立冻结结果。
- 分歧入队后 assignment 释放，B 可继续领取别的任务，但该音频仍不可复标/纠正/训练导出。
- 管理员选 original、secondary、edited annotated、edited skipped 全部真实跑通。
- 管理员 edited 最终稿能通过扩展后的既有接口撤销并恢复；不会伪造 submitter，不能误放宽普通用户版本的恢复条件。
- 两份原始提交的文本和核验历史保持不变；edited 为独立新版本，管理员真实身份可追溯。
- stale round revision、错误原稿/复标稿版本、错误状态、同 operation ID 不同请求都失败且不留半成品。
- 同一次 save/complete/decision/cancel/config 反复重放，仅产生一次状态变更及工作量记录。
- 两个管理员竞争裁定，只能一个成功；管理员裁定与撤销竞争不产生失效发布。
- awaiting_review 不能通过 cancel、关闭配置、退出或 session 到期解锁训练。

### 13.6 并发与恢复

- 20 个不同用户并发 claim：每人一条、每音频一条在途，不可能两个复标者同时编辑同一轮。
- 同一用户两个并发请求恢复同一任务，随机分流不会生成两条工作。
- 复标领取与原作者 reopen 竞争；完成与管理员 release/revoke/deactivate 竞争；保存与 session takeover 竞争。
- 比较前读取快照，最终提交前 revision/原版本/轮次状态改变，必须返回冲突而非写过期指标。
- 涉及两名用户的管理操作锁序一致，在受控等待测试中无循环死锁。
- 事务中途异常回滚后，没有孤立 round、草稿、导出阻塞或已计入的工作量。
- 未完成复标在重启后可恢复；旧会话或旧 lease token 不可写入。

### 13.7 统计、导出与备份

- 一条 60 秒音频由 A/B 各做一次，全局 annotated_duration 始终为 60 秒；B 工作量可以为 60 秒。
- 两条不同的 60 秒音频合计 120 秒，防止 SUM(DISTINCT duration) 的错误实现。
- 同一 task 多来源、多版本、多轮次、多事件仍只记一次；总计不能从人员工作量相加。
- 自动通过不改变原日期桶；管理员采用后按唯一有效版本重新归属，不在两个日期或两名有效成果归属中重复计算。
- waiting 与 in_progress 均从实际训练 tar 中排除；通过/裁定后只输出最终版本，原/复标两个版本不会同时成为训练样本。
- 排名资格、小时门槛、片段、excluded 统计和旁文件一致；管理员编辑稿不因 submitter=NULL 丢失。
- 导出快照前后的 claim/decision 竞争符合第 9 节定义，快照一致性可验证。
- Excel 一条音频一行，审计 JSON/完整数据库备份保留待质检数据。
- dump→新隔离库 restore 后比较两份文本、round 状态/证据、最终版本、参与者、配置、assignment 及审计；重启读取和完成剩余流程成功。
- 现有元数据导入导出、重复发布导出、管理员统计、预处理保护及场景权限测试继续通过。

### 13.8 测试命令与环境

所有 Python 命令继续通过 uv。先运行新增后端测试，再在 PostgreSQL 16 与 18 上分别运行完整现有套件。仓库 README 当前的命令为：

```bash
# 新增用例；具体文件名落实后在报告中列出
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q tests/test_cross_check_*.py

# 完整测试：默认测试夹具提供隔离 PostgreSQL；报告真实版本
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q

# PostgreSQL 18，使用实际安装的二进制路径
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q
```

路径以实施环境实际安装为准。不能把同一主版本跑两遍称作 16/18 矩阵，也不能连接生产 DSN 进行测试。尽管本轮不开发前端，现有 Playwright 回归仍运行，以验证默认关闭及旧接口行为未损坏；不为新增质检设计页面或 UI 测试。

### 13.9 容量与查询计划

在仓库现有 100k 合成测试框架上增加本功能场景，使用独立临时库，记录硬件、PostgreSQL 版本、Gunicorn worker/thread 配置与实际样本数。

- 数据同时包含历史 annotated、普通 pending、多来源、多场景、多人参与、已覆盖版本、开放轮次和缺基线异常，不能只测很小的“所有数据都可领”表。
- 测试关闭/10%/100% 配置；20 用户并发领取，分别记录普通领取、复标领取、恢复和锁忙样本，不把恢复请求冒充新领取。
- 对全场景、指定场景、稀疏场景、空场景及多来源组合保存真实 `EXPLAIN (ANALYZE, BUFFERS)`；证明人数/版本/轮次索引和来源过滤实际参与查询。
- 首版目标：同一机器上新复标 claim 的 p95 不高于 1 秒，普通 claim 相比关闭配置的同基线 p95 退化不超过 20%。报告实测 p50/p95/p99、吞吐和锁等待；未达标须优化后复测，不能只给静态 SQL 推测。
- 分别记录 100、1000、3000 词比较的耗时/峰值内存，以及超过预算的转人工行为；验证比较计算不长时间占有任务/用户数据库锁。
- 验证全局 overview、pool、Admin 队列和训练选择查询没有版本/轮次连接导致的行数膨胀。
- 固定随机种子只用于可复现测试，生产使用服务端正常随机源。抽样分布作为容量报告证据，不使用不稳定的小样本频率断言作为 CI 通过条件。

## 14. grok build 最终交付清单

- [ ] 新迁移、历史回填、数据库约束和索引完整；不改写旧迁移。
- [ ] 新配置及所有上述 API 可调用，提供普通用户与 Admin 的完整请求/响应示例。
- [ ] 随机发生在 claim；历史成果可抽中；当前场景、scope 和他人限制均由后端执行。
- [ ] 从 baseline 独立复标，所有读取路径不泄露对照结果。
- [ ] 比较严格执行 worddiff_v1 与 >10% 规则，异常不会自动通过。
- [ ] 自动通过、管理员三种裁定、取消/失效及管理操作联动全部完成。
- [ ] 全局音频数/总时长去重；有效成果归属与实际工作量分开。
- [ ] 后续训练导出阻塞和最终版本选择真实生效，审计/备份不丢待质检数据。
- [ ] Session/revision/幂等/并发测试、PostgreSQL 16/18、既有浏览器回归及容量验收有实际结果。
- [ ] 前端 HTML/CSS/JavaScript 未增加质检 UI/UX 改动。
- [ ] 开发报告写入 `docs/plans/cross-annotation-quality-backend-development-report.md`，包含业务提交、schema 版本、API 示例、测试命令与真实结果、容量产物、迁移/回退步骤及剩余限制；未完成项明确标注，不用“前端以后再做”掩盖后端缺口。
