# 场景、来源置信度与任务分配：开发实施计划

日期：2026-09-14。状态：已在分支 `codex/scene-provenance` 按阶段落地（P0–P7 合成验收）。真实 ASR 与生产数据迁移仍延期。
开发基线：`55672095e6007407b15a7850f03be8752a31fb27`，标签 `baseline-2026-09-14`。

## 1. 交付目标与业务约定

本轮交付三个完整闭环：采集元数据可追溯入库；标注与管理页面可查看、核验、筛选和统计；任务可以按场景分配且优先领取来源置信度高的音频。

来源判断、模型分类和人工核验分别保存，任何一种结果都不能覆盖另外两种。来源置信度使用 `high / medium / low / unknown`；当前爬虫实际产生 high、medium。它表示来源与场景的关联程度，不是 ASR 准确率、语言识别分数或已校准概率。

以下两项为本计划的推荐默认值，已向用户征询偏好，未收到答复前不视为用户已确认：

- 标注员随任务提交人工核验；管理员可以审计和修正。
- 管理员可以限定每位标注员可领取的场景；标注员在允许范围内选择。已有用户默认保持全部场景可领。

其他明确规则：

1. 一个音频任务可以关联多个来源场景、多个批次和多个来源证据。
2. 置信度属于具体来源记录。例如“机场 high、购物 medium”必须保留两条关联。
3. 场景筛选和置信度排序以来源场景为依据；模型预测和人工核验有单独的筛选维度。来源记录不会因人工否定而被改写。
4. 已领取任务优先恢复。改变选择器或管理员修改场景范围，不隐式释放已有任务。
5. 首版核验对象是整条音频，可选择一个或多个场景；多场景不等于每一段语音都属于所有场景。
6. 旧任务没有来源信息时显示“来源场景未知 / 来源置信度未知 / 场景待核验”。不根据目录名、旧 category 或标注完成状态伪造可信来源或人工确认。
7. 为保证兼容，本轮场景核验不是完成任务的硬性必填项；未核验可以提交，并进入管理员待核验筛选。以后可以按批次开启必填规则。

## 2. 当前实现与改动入口

| 当前文件 | 已有能力 | 本轮改动 |
|---|---|---|
| `preprocess.py` | VAD、ASR、失败恢复与入库编排 | 读取规范化元数据，调用共享入库服务；先同步已有任务来源，再决定是否跳过 ASR |
| `preprocess_store.py` | 保护人工内容、创建 baseline/draft | 保留旧入口，增加类型化输入适配；来源写入与文本重处理分别控制 |
| `classify.py` | 从转写生成单个 category | 写独立预测历史，记录输入版本、模型、提示版本与可选分数 |
| `annotation_repository.py` | 原子领取、保存、发布、撤销、管理查询 | 委托新增模块处理来源、核验、过滤和排序；保留核心事务不变量 |
| `server.py` | API、Session、Admin/CSRF、媒体鉴权 | 新增元数据 Blueprint；扩展现有 assignment、completed、admin 响应 |
| `index.html`、`completed.html` | 标注及本人完成记录 | 来源信息条、详情、场景选择与核验操作 |
| `admin.html`、`admin.js` | 分类、状态、质量与审计 | 新筛选、统计、来源详情、核验修正和人员场景配置 |
| `manage_state.py`、`export.py`、导出脚本 | JSON 兼容导出、Excel、训练数据导出 | 加入版本化元数据旁文件与导出字段，保留旧格式兼容性 |
| `db.py`、`migrations/` | 事务迁移、启动 schema 精确检查 | 新迁移、索引、迁移及兼容回退演练 |

爬虫的 manifest 和同名 JSON 已保存 `scene`、`confidence`、`confidence_basis`、`video_id`、`url`、`source_type`、校验和与核验标志。当前预处理没有消费这些字段，领取接口也没有返回它们。

新采集目录为 `/home/ck/ar_audio/0914`，当前网站根目录为 `/home/ck/ar_audios`。导入必须显式处理根目录映射，不能只改数据库路径或直接替换整个网站根目录。

## 3. 架构与模块边界

保留 Flask、psycopg、PostgreSQL、原生前端及现有迁移机制。新增一个按功能组织的包，应用层协调事务，仓储层执行 SQL，适配器只处理外部格式。

```text
crawler manifest / sidecar / legacy input
                 │
                 ▼
        adapters → contracts + taxonomy
                 │
                 ▼
       ingestion service / review service
                 │   接收同一个 conn / cursor
                 ▼
         repositories + shared queries
                 │
                 ▼
               PostgreSQL

Flask API → serializers → shared frontend metadata components
                   ↑
        same query filters / claim policy
```

建议目录：

```text
annotation_metadata/
  contracts.py        # Pydantic 输入/输出模型；类型和错误位置
  taxonomy.py         # 稳定场景代码、别名映射、标签与状态规则
  adapters.py         # manifest、sidecar、旧调用适配
  ingestion.py        # 元数据导入与幂等编排
  repository.py       # 来源、身份、预测和核验记录存取
  queries.py          # 来源匹配、统计与分页使用的共同条件
  claim_policy.py     # 可信度优先 / 原顺序策略
  reviews.py          # 核验与管理员修正命令
  serializers.py      # task metadata 的唯一序列化入口
  routes.py           # Blueprint；不反向 import server
static/
  metadata.js         # 公共标签、来源详情、核验控件
  metadata.css
tests/
  test_source_import.py
  test_scene_claims.py
  test_scene_reviews.py
  test_metadata_queries.py
  test_metadata_migrations.py
  browser/
```

首轮不拆完整个 `annotation_repository.py`。新功能调用明确的小模块，旧公共函数作为兼容外观逐步委托；只有被本轮改动触及的重复逻辑才抽出。子仓储不自行开启独立连接或提交，否则“完成文本＋核验＋释放 assignment”的原子性会被破坏。

新增 Blueprint 在 `server.py` 显式注册，通过注册函数传入现有鉴权装饰器，避免循环导入和复制鉴权。沿用 Flask 原生模块组织能力。[Flask Blueprints](https://flask.palletsprojects.com/en/stable/blueprints/)

新增边界校验建议使用 Pydantic v2，并由 uv 锁定经验证版本。API 请求拒绝未知字段；采集输入保留未知扩展字段到原始记录，但不自动把它们赋予业务含义。Pydantic 负责结构校验，数据库外键、唯一约束和事务负责最终一致性。[Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)、[validators](https://docs.pydantic.dev/latest/concepts/validators/)

## 4. 数据模型

### 4.1 来源、预测、核验分别建模

| 实体/表 | 关键字段与约束 | 用途 |
|---|---|---|
| `scenes` | `code` 主键、中文/英文标签、active；别名在适配层映射 | 场景代码稳定，不从目录编号推导主键 |
| `source_batches` | UUID、唯一 `batch_code`、名称、来源说明、创建时间 | `crawler-2026-09-14-0914` 等全局唯一代码；不能仅用每年会重复的 0914 |
| `source_import_runs` | batch、清单快照 SHA、契约版本、状态、处理数、错误报告、断点 | 记录增量导入、重试和每次读取的清单版本；不混用旧 JSON 迁移批次表 |
| `task_media_identities` | provider、external ID、variant、task ID；身份组合唯一 | 同一视频的相同音频表示只创建一个新任务，多个场景共享它 |
| `task_sources` | task、batch、scene、confidence、basis、source_type、URL、video/channel 信息、原始记录、记录版本 | 一个任务对应多条来源证据；核心字段独立存储，未知扩展保留 JSONB |
| `task_scene_predictions` | task、输入 annotation version/revision、输入摘要、模型/提示版本、预测标签、可空 score、时间 | 保留模型历史；按输入版本判断是否过期，不冒充来源置信度 |
| `scene_reviews` | annotation version、review_no、状态、备注、actor、operation ID、时间、前一 revision | 追加式人工核验历史；与标注版本关联，不能覆盖来源或模型预测 |
| `scene_review_labels` | review ID、scene code 组合主键、场景外键 | 一个核验结果可选多个场景，保留引用完整性 |
| `annotator_scene_scopes` / `annotator_scene_access` | 用户、模式 all/restricted/none、revision、允许的 scene | 人员分配范围与乐观锁；空列表不能被误解释成全部 |

`annotation_tasks` 和 `annotation_versions` 继续作为任务及文本版本的主实体。`assignments` 增加领取上下文：选定场景、匹配来源记录 ID、策略版本与当时置信度。发布事件记录当时的核验 revision ID，以便区分“提交时结果”和“之后管理员修正结果”。

新增 ASR 并发占位使用 task 上的处理 token、租约截止时间和处理版本；获取、续期和提交均校验 token。过期处理者不能覆盖接管者的结果，也不能释放新租约。它只保护同一任务的预处理，不承担网站 assignment 的职责。核验状态和来源置信度使用可扩展的文本值＋数据库 CHECK；模型 score 允许 NULL，非空时按供应方约定校验范围并保存分数含义。

场景目录现为十个稳定代码（显示顺序）：`restaurant`、`hotel`、`taxi`、`airport`、`clinic`、`tourism_information`、`emergencies`、`spoken_languages`、`business_negotiation`、`shopping`。英文平台标签与该顺序一致。映射 `01_机场` 等采集别名；未知来源（无当前来源、NULL `scene_code`、以及兼容 `source_scene=unknown`）归入 `spoken_languages`。模型预测与人工核验的 `unknown` 仍表示缺省结果，不因来源回退而改写。

### 4.2 来源版本、去重与冲突

- `task_sources` 使用稳定 `record_key`、递增 revision 和 `is_current`。当前记录有部分唯一索引 `(batch_id, record_key) WHERE is_current`。
- 同一规范化记录重导是无操作；来源内容改变则追加新版本，并在同一事务中将旧版标为非当前。旧业务字段不就地覆盖。
- 记录摘要排除爬虫每次刷新的时间戳等易变信息，避免一次无意义时间更新产生新证据版本。
- 业务查询只使用当前证据，领取事件可继续引用不可变旧记录，解释“当时为什么选中”。
- 新音频按 `(provider, external_id, variant)` 去重；本批次 variant 表示完整 16kHz PCM 音频。PCM SHA 用来校验身份一致性，同 ID/variant 不同内容进入冲突报告。
- 不同视频 ID 即使 PCM 相同，首版只报告潜在重复，不自动合并；历史上已分别标注的任务也不自动合并或删除。
- 无外部 ID 的旧任务仍按规范化 rel_path 匹配。可验证的旧任务身份回填使用独立 dry-run 工具；无法确定时保持 unknown。
- 来源记录有 scene/confidence/batch 复合索引及 task/current 索引；预测按 task/时间索引，核验按 version/review_no 唯一索引。具体列序以负载测试和查询计划决定。

### 4.3 category 的兼容处理

保留原 `category` 字段和旧 API 字段，明确标记为 legacy 分类；不回填成 source scene。新的 `classify.py` 写预测表，必要时在同一事务更新旧 category 作为兼容展示缓存；新界面读取独立预测对象。模型只返回标签时 score 保持 null，不生成虚假的 0.9。

预测保存输入转写对应的 version/revision 或摘要；文本发生变化后显示“预测基于旧版本”，不当作当前人工结论。该分层参考 Label Studio 对任务输入、预测、人工标注和 model_version 的区分。[预测导入格式](https://labelstud.io/guide/predictions)、[任务模型实现](https://github.com/HumanSignal/label-studio/blob/develop/label_studio/tasks/models.py)

## 5. 入库流程

流程：**清单快照 → 解析/标准化 → 路径与音频验证 → 身份匹配 → 来源同步 → 必要的 VAD/ASR → 完整性验证 → 可领取**。

1. CLI 显式指定 `batch_code`、manifest、采集根目录及网站音频根目录。不能直接信任清单中原机器的绝对 `audio_filepath`。
2. 对正在增长的 JSONL 读取完整行快照，记录快照摘要和截止字节；尾部未完成行下轮处理，中间坏行写错误报告。根清单与场景清单不重复消费。
3. 只接收 `status=ready` 且文件存在、解码/时长/格式符合要求的记录。manifest 与 sidecar 核心信息不一致时报告冲突，不按读取顺序覆盖。
4. 使用 Path.resolve 验证路径位于允许根目录内，拒绝 `..` 和越界符号链接。规范化 URL 只接受 http/https，保存链接不意味着服务端自动抓取链接。
5. 导入后网站使用自身根目录下的 canonical rel_path；先完成受控复制或同文件系统硬链接、校验及原子改名，再将任务标为可用。源目录和爬虫的在途文件不移动。
6. 对已有 task，可只补来源，不修改 segments、waveform、人工草稿、published 版本或 assignment。这个分支必须在旧的“已预处理/已有人工内容则跳过”判断之前运行。
7. 新任务和外部身份占位一起创建，先 `eligible=false`；进程崩溃留下的未完成任务可继续恢复。唯一身份冲突时重新读取已存在任务，而不是制造重复任务。
8. VAD/ASR 在数据库事务外运行。进入短事务时重新检查任务是否已有人工内容，再写文本、波形和来源；ASR 未完成或被拒绝时保持不可领取。
9. 两个导入进程对同一身份竞争时使用数据库唯一约束及短事务保护。外部 ASR 调用只允许一个处理者取得明确的处理租约，租约到期可重试；保存前仍重验人工保护条件。
10. 输出 created、metadata_updated、unchanged、protected_text、conflict、invalid、asr_pending 等可审计结果。重跑同一清单不重复任务，不增加无意义来源版本。

外观接口以 `PreprocessedTaskInput` 等对象替代继续加长参数列表，旧 `store_preprocessed_task(...)` 适配到该对象，现有调用方分阶段迁移。事务边界沿用 psycopg context manager，不新建自定义事务框架。[psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)

预计 CLI（待实现，不是现在可执行的命令）：

```text
manage_state.py inspect-source-manifest --manifest ... --batch-code ... --audio-root ...
manage_state.py import-source-metadata --manifest ... --batch-code ... --dry-run
preprocess.py --manifest ... --batch-code ... --source-audio-root ... --audio-dir ...
manage_state.py verify-source-metadata --batch-code ...
```

## 6. 领取与场景范围

### 6.1 过滤语义

- 用户身份和允许范围由服务端读取，不信任前端传来的用户 ID 或允许场景列表。
- `all` 可领取任何来源场景和 legacy unknown；`restricted` 仅可领取配置场景；`none` 不可领取新任务。是否允许 restricted 用户领取 unknown 使用独立布尔项，默认 false。
- 用户选择一个场景时只领取该场景；不选择时表示其允许范围内的全部。所选场景没有任务时返回明确空池原因，不静默改领其他场景。
- source scene、confidence、batch 组合条件必须作用于同一条有效来源证据。不能用“机场 medium”记录匹配场景，再用另一条“购物 high”记录匹配置信度。
- pool counts、候选选择、管理员列表和分组统计复用同一个类型化过滤对象及来源匹配函数；列表额外加 status 等条件，不能各写一套场景解释。

### 6.2 排序及事务

顺序：**恢复已有 assignment → 匹配范围内的本人保留任务 → high → medium → low → unknown → allocation_order → task_id**。

已有 assignment 的恢复先于新请求场景筛选。场景范围调整只影响下一次领取，若需立即收回必须使用现有可审计回收操作。迁移保留任务仍必须满足新的领取范围；保留状态不构成跨范围授权。

优先级来自当前筛选范围内最可信的来源记录。未选择场景时，只在用户允许的场景集合中取最高优先级；记录选中的证据与场景。多条等价证据用稳定的场景代码/来源 ID 排序。

实现继续使用现有用户行锁、`FOR UPDATE OF t, v SKIP LOCKED`、assignment 双唯一约束及插入冲突重试。布尔保留优先级显式 `COALESCE(..., false)`，避免 NULL 排序改变优先级。来源聚合放在子查询，最终只锁可识别的 task/version 行，不能在聚合结果上直接加行锁。[PostgreSQL SELECT/locking](https://www.postgresql.org/docs/18/sql-select.html)

保留现有用户行锁作为同用户写操作的串行化入口。在 P1 为领取、保存、发布、撤销、停用、来源更新和新增核验操作列出锁顺序矩阵；新增路径优先采用用户 → 已有 assignment（如有）→ task → version，批量操作按稳定 ID 顺序锁定。现有管理路径并非全部按同一书面顺序取锁，必须逐条核对并验证共享用户锁保护范围，不能直接假定没有死锁。来源更新先锁 task，场景权限修改先锁用户。候选加锁后重验匹配证据及状态，处理读取候选期间发生的元数据更新。

`SKIP LOCKED` 的优先保证针对当前可取得的候选；高优先任务被其他事务锁住时可以领取较低优先任务，不承诺跨并发请求的绝对全局排序。前端可用数是查询时快照，不能承诺下一次点击一定领到。

空池原因至少区分 `no_matching_scene`、`no_scene_access`、`no_preprocessed`、`temporarily_busy`、`temporarily_all_assigned`、`all_completed`，保持原错误码兼容。

保留两种策略：`source_confidence`（新功能默认）和 `fifo`（兼容/受控回退），策略仅生成可信的排序计划，不掌管身份校验和事务。客户端不能提交任意 SQL 或类名。首版不实现配额、加权随机或自动老化；管理员可按 medium 批次查看积压，后续确有需求再增加策略。

## 7. 人工核验与现有版本机制

状态：`pending`、`confirmed`、`mixed`、`out_of_scope`、`uncertain`。confirmed 要求一个场景，mixed 要求至少两个，out_of_scope 不选择十场景标签；uncertain 可记录候选和原因，不能计为已确认。

- 核验随现有草稿自动保存，使用同一 `expected_revision` 和 `operation_id`；只有核验实际改变时追加 review revision，普通转写保存不制造核验历史。
- 完成请求把最后一轮转写、核验、published 切换、事件和 assignment 释放放进同一事务。保存操作哈希包含核验字段，重试不能重复提交。
- 默认核验没有填写时保持 pending；旧客户端不传核验字段表示不更改，不表示清空。
- 每条 review 关联 annotation version。有效结论取 `current_published_version_id` 下最新有效 review；尚在草稿的核验只能显示为“本人已保存，未提交”，不进入正式已核验统计。
- 纠正草稿可以展示前一版本核验作为参考，但状态设为待复核；发布新版本后才切换正式核验。放弃纠正继续显示原 published 版本。
- 管理员修正采用独立命令：要求当前 published version、expected review ID、operation ID、原因与现有 Admin/CSRF 权限；在同一事务追加 admin review revision 和 audit，不改转写内容、原提交者或贡献时长。
- 存在活跃纠正草稿时，管理员修正返回冲突提示，避免与标注员并行修改同一核验结论；管理员可先处理既有 assignment。
- 发布事件保留提交时 review ID，管理员详情能区分原提交核验与后续修正；旧 review 始终可追溯。
- 撤销/停用使旧 published 版本退出当前统计；从 baseline 新建草稿时核验恢复 pending。恢复历史版本时重新采用该版本关联的有效 review。
- 新来源或重新分类不改变人工核验。新增来源与已核验结果冲突时显示差异，管理员再决定是否复核。

现有用户身份体系保持不变，场景范围是任务分配控制；不能把用户名登录扩展宣称为强身份认证。权限和审计沿用平台现有身份语义。

## 8. API 合同

增加结构化对象，原字段保持兼容。共用 serializer 覆盖领取、恢复、本人完成详情、历史详情和管理员详情，避免同一任务在不同页面显示不同信息。

```json
{
  "metadata": {
    "schema_version": 1,
    "sources": [
      {
        "scene_code": "airport",
        "scene_label": "机场",
        "confidence": "high",
        "confidence_basis": "explicit_video_in_user_document_not_content_verified",
        "provider": "youtube",
        "video_id": "example-id",
        "source_url": "https://www.youtube.com/watch?v=example-id",
        "batch_code": "crawler-2026-09-14-0914",
        "source_type": "explicit_video"
      }
    ],
    "prediction": null,
    "scene_review": {"status": "pending", "scene_codes": []},
    "claim_context": {"scene_code": "airport", "confidence": "high"}
  }
}
```

接口建议：

| 接口 | 行为 |
|---|---|
| `GET /api/scenes` | 返回可选场景及显示标签，标注员只能看到自己的可领范围 |
| `GET /api/assignment?source_scene=airport` | 有 assignment 则恢复；否则返回该范围池状态 |
| `POST /api/assignment/claim` | 可选 `source_scene`、`batch_code`；旧 `{}` 请求继续可用 |
| 现有 PATCH save / POST complete | 增加可选 `scene_review`，复用 revision/idempotency |
| 现有 admin tasks/overview/annotations/quality | 统一接受 source_scene、source_confidence、batch_code、review_status；model/human 场景用不同字段 |
| `GET /api/admin/metadata/facets` | 场景/批次选项、统计口径、应用中的筛选条件 |
| `POST /api/admin/annotations/<id>/scene-review` | 追加管理员核验修正及审计 |
| `PUT /api/admin/annotators/<id>/scene-scope` | 保存范围、expected scope revision、操作 ID 与原因 |

非法枚举返回 400，超出场景范围返回 403，陈旧 revision/权限版本冲突返回 409；字段错误使用稳定 code 与 field path。来源详情只暴露允许的字段，原始清单中的绝对路径与任意扩展不直接发给标注员。

## 9. 前端与统计口径

### 标注页及本人完成页

任务标题下新增：**机场 · 来源置信度高 · 场景待核验**。来源信息可展开查看判断依据、视频链接、批次及其他场景关联。新信息按用户示例中文展示，场景字典保留英文标签供后续语言切换。

空闲页提供十场景选择器和匹配任务数（含 Spoken languages，覆盖未知来源）；有未完成任务时保留当前任务，清楚标出下次领取偏好。核验控件提供确认、多场景、不属于十场景、无法判断。信息提示明确来源分类不是人工核验。平台界面为英文。

复用 `static/metadata.js` 的纯格式化函数与 DOM 组件；现有内联脚本可通过动态 import 调用，避免一次性把依赖全局 onclick 的页面改成模块。`server.py` 为独立静态目录提供受限路径的资源路由。

来源标题、备注用 textContent 输出；链接协议校验并设置 noopener；状态用文字与样式共同表达。保存冲突、断网恢复、移动端和键盘操作都进入浏览器验收。

### 管理员页

新增来源场景、来源置信度、批次、核验状态筛选；模型分类和人工核验场景另列，避免一个“场景”控件混合三个含义。任务表与详情展示对应信息，人员详情提供范围配置。

统计统一遵守：

- 总任务数、音频时长按唯一 task 计数，先构造唯一任务集合，再聚合；不能直接对来源 JOIN 的展开行求和。
- 场景分组中，同一任务可以在多个场景出现，界面注明分组可重叠，分组相加不等于全局总数。
- 置信度分布按当前场景/批次上下文下的最高有效来源置信度互斥分桶；缺少来源单列 unknown。匹配条件必须落在同一条证据上。
- 已核验统计只取当前 published 版本的有效核验；待核验明确区分未发布任务和已发布但未核验任务。
- 原始音频时长、已有可训练时长、场景关联音频时长分开命名。音频级场景不能推出精确的“该场景可训练语音时长”。
- 列表、图表、可导出数据返回相同 applied_filters；切换过滤条件重置分页 cursor，cursor 绑定规范化过滤摘要，避免跨条件续页。
- 同一概览响应需要一致读快照，页面多次请求标明 as_of；不承诺实时变化的多个请求天然是同一快照。

## 10. 设计模式的实际取舍

| 模式 | 本轮使用方式 | 扩展边界 |
|---|---|---|
| Strategy | 可信度优先与 FIFO 两种排序策略 | 以后可添加配额/老化；事务及权限始终在统一领取服务 |
| Adapter | crawler manifest、sidecar、旧参数调用转为统一 DTO | 对接新采集器时增加适配器，不修改保存/领取流程 |
| Facade | 入库服务和核验服务提供少量稳定用例入口 | CLI、HTTP、测试共享业务入口 |
| Command | 复用 operation ID、request hash、事务、审计封装核验修正和范围更新 | 重试返回相同结果；不创建通用命令总线 |
| Decorator | 复用现有登录、Admin/CSRF 和错误映射装饰器 | 外部调用重试使用现有机制；不对整个数据库事务盲目重试 |
| Builder | 类型化输入对象、pytest 参数化 fixture 组合复杂测试数据 | 生产端通常直接构造模型，不增加链式 Builder 类 |
| Factory | 少量显式注册表按 input_format/policy 选择已有实现 | 不做自动插件扫描或依赖注入容器 |
| State | 枚举＋集中状态迁移校验函数 | 当前规模不引入每状态一个类或状态机库 |
| Chain of Responsibility | 校验按明确函数流水线执行，产生结构化错误 | 没有动态处理链需求，不建立 handler 类层级 |
| Template Method | 用显式流水线函数与可组合适配器表达固定步骤 | 不使用继承式框架；避免插件改写事务骨架 |
| Observer / 发布订阅 | 复用数据库中的提交/管理事件，成功提交后刷新页面 | 需要异步消费者时再评估 outbox；本轮不加消息队列 |
| Proxy | 延用 Nginx 鉴权后的内部音频代理 | 此功能无需新增代理抽象 |

## 11. 开源与框架参考清单

参考现有实现的职责划分与接口合同，不直接迁入其全套框架。开发时锁定采用的依赖版本；如复制具体代码，记录源提交及适用许可证。

| 参考 | 本项目采用的部分 |
|---|---|
| [Label Studio 预测格式](https://labelstud.io/guide/predictions) | task 输入、prediction、人工标注独立；模型版本和分数具有明确语义 |
| [Label Studio tasks/models.py](https://github.com/HumanSignal/label-studio/blob/develop/label_studio/tasks/models.py) | 检查任务与预测、锁定、不同展示上下文的边界；不替换本项目已验证的 SQL 领取 |
| [Flask Blueprint](https://flask.palletsprojects.com/en/stable/blueprints/) | 模块化路由注册和资源组织 |
| [Pydantic models](https://docs.pydantic.dev/latest/concepts/models/) | 类型化边界模型和 JSON Schema；避免手工重复检查每个 API 字段 |
| [psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) | 共享连接、短事务、失败回滚 |
| [PostgreSQL locking](https://www.postgresql.org/docs/18/sql-select.html) | 多消费者任务选择、SKIP LOCKED 的适用范围和限制 |
| [pytest fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html) | 组合数据 fixture、隔离数据库与参数化场景 |
| [Playwright pytest](https://playwright.dev/python/docs/test-runners) | 真实浏览器验证页面、请求、冲突和交互；仅加入测试依赖 |

无需迁移到 Django/FastAPI、引入 ORM/Alembic 或增加 Redis/Celery，现有技术栈足以支持本轮需求。

## 12. 分阶段开发及验收

所有开发从独立基线建立干净 worktree/checkout，分支使用 `codex/scene-provenance`。不从本地旧历史分支直接推送，以免混入大体积历史。每阶段提交可审查的代码、迁移和测试；集成通过后再形成上线版本。

| 阶段 | 工作内容 | 完成标准 |
|---|---|---|
| P0：合同与兼容设计 | 确定两项产品偏好、字段语义、状态和 API；建立真实清单脱敏样本 | 来源/模型/人工字段不混用；多来源、unknown、旧请求和回退合同清楚 |
| P1：模型及迁移 | 新表/索引/约束；类型化合同；共用 serializer；兼容构建 | 空库和旧库迁移通过，旧任务/版本/assignment 不变，回退构建可启动 |
| P2：导入闭环 | 清单适配、去重、路径映射、元数据单独同步、ASR 对接 | 同一清单多次/并行导入无重复；人工内容保护；失败能续跑 |
| P3：读取与统计 | API 元数据、共用筛选条件、管理员分组及导出 | 多场景/批次组合过滤正确，无 JOIN 重复计数；分页与过滤一致 |
| P4：人工核验闭环 | 草稿保存、发布、管理员修正、撤销恢复、审计 | 核验和文本原子提交；历史不覆盖，旧客户端不清空核验 |
| P5：范围和领取 | 服务端场景权限、策略、pool state、领取上下文 | 单/多场景正确，优先级符合合同，并发唯一性及续领不回归 |
| P6：完整页面 | 标注/本人完成/admin 共用组件、操作反馈、配置和图表 | 浏览器走完导入→领取→核验→完成→筛选→管理员修正流程 |
| P7：云端验收与迁移准备 | PostgreSQL 16/18 验证、容量测试、恢复演练、运维文档 | 全部验收门槛通过，再安排真实数据迁移及正式入口切换 |

阶段可按依赖细分为多个提交/PR；P6 可以先基于冻结的 API 样例做组件，最终必须与真实 API 集成验收。本计划不以未测过的工期承诺替代验收门槛。

## 13. 测试与性能验收

基线已通过 69 项测试。它们作为回归基础，新增测试重点验证真实业务不变量，而非重复实现内部细节。

| 层次 | 必测内容 |
|---|---|
| 合同/适配 | 十场景别名、来源 unknown→Spoken languages、非法等级、外部字段保留、manifest/sidecar 冲突、缺失文件、越界路径、批次代码 |
| 入库 | 同记录重复导入、清单追加、来源修订、同视频跨场景/批次、身份冲突、并发导入、崩溃恢复、ASR 被拒绝仍保留来源 |
| 人工保护 | 已领取/已编辑/已发布任务只补来源；metadata-only 不调用 ASR、不改 segments 或 assignment |
| 领取 | high 优先、同级稳定顺序、本人保留任务、不同场景、超范围/空范围、未知来源、禁止重领、锁冲突、退出/重启后恢复 |
| 并发 | 20 个用户抢一个/一批任务；同用户并发领取；范围变更与领取；来源变更与领取；管理撤销与保存 |
| 核验 | 陈旧 revision、幂等重放、完成原子性、草稿不影响 published、管理员修正冲突、撤销/停用/恢复语义 |
| 查询 | 同一证据满足组合筛选、多来源去重时长、可重叠场景分组、旧 category 独立、过滤 cursor、SQL 注入式参数 |
| 导出/恢复 | PostgreSQL dump/restore；元数据导出/导入 round-trip；旧 JSON 兼容、来源和核验历史校验 |
| 浏览器 | 信息条、筛选、权限范围、核验保存、断网/409、音频 Range、键盘操作、移动布局、XSS 文本 |

数据库集成测试保留本地隔离 fixture；CI 额外跑真实 PostgreSQL 16/18 服务矩阵，不能把 pgserver 自带版本的通过等同于两个生产目标版本都通过。外部 ASR 在自动化测试中使用固定响应，真实服务只做受控小样本验证。

负载样本：10 万 task、约 30 万来源关联、20 个并发用户，覆盖 high 占比很高/很低、窄场景、空场景和锁竞争。复用并更新 `scripts/load_test_100k.py`，补齐当前 baseline/draft 数据不变量后扩展，不另造压测框架。

初始性能验收目标（目标，不是当前测量结论）：在云端 4 核/8GB 测试机，正常候选分布下领取 API p95 ≤ 500ms，50 条列表 p95 ≤ 500ms，10 万任务概览 p95 ≤ 2s。记录吞吐、p95/p99、锁等待、错误率和查询计划；不靠放宽阈值掩盖全表排序或 N+1。

先用合适索引、分页和一次性批量读取。只有实测仍不足时才加入可重建的场景优先级投影，并让投影更新与来源变更同事务；首版不建立异步缓存一致性系统。

## 14. 数据迁移、导出和回退

1. 只做追加迁移，保留 task/version/user ID、category、路径、已有标注及 assignment。旧来源保持未知。
2. 每个迁移按现有 runner 在事务内执行；大型 `CREATE INDEX CONCURRENTLY` 不放进事务迁移，必要时作为有检查点的独立运维步骤。
3. 来源回填与 schema 迁移分离，使用可审查 dry-run 报告、小批事务和幂等断点。目录推断只能生成候选，不能自动提升置信度。
4. PostgreSQL custom dump 继续作为完整恢复来源。旧 `export-json` 保持兼容；新表另导出版本化 metadata 文件，包含来源历史、预测、核验、范围及必要的身份映射，导出和恢复校验都覆盖这些文件。
5. Excel 增加来源场景/置信度/批次和人工核验列。训练数据 `data.json` 保持已有字段合同，元数据写旁文件并引用 task/segment，明确是音频级场景信息。
6. 云端新代码先使用合成/小样本测试库，不将正式数据写入现有演示库。正式恢复用新建库，迁移后逐项核对任务数、版本、segments、assignment、来源关联和核验历史。
7. 当前音频已超过云端剩余容量，且新爬取数据还在增长。真实迁移前重算音频＋数据库＋备份＋临时空间，扩容容量以届时实测为准。

**特别处理 schema 精确检查：**`assert_schema_current()` 要求已应用版本与代码中迁移列表完全一致。新库应用迁移后，直接回到 `5567209` 会健康检查失败；不能把“切回 Git 标签”作为数据库迁移后的回滚方案。

因此在 P1 建立并测试兼容回退构建：它携带新 migration 文件、具备对新数据安全读取的能力，同时关闭新领取策略和编辑入口；不对未知新表做删除。该构建与新 schema 必须一起通过启动、读旧/新任务、续领和导出测试。若不引入宽松 schema 版本范围，发布与 schema 变更使用一次短维护窗口：停服务 → owner 迁移 → 启动对应版本 → 健康/冒烟验收。

回退开关分别控制元数据展示/编辑、核验编辑和排序策略；关闭排序策略不关闭场景访问范围校验。回退期间涉及新格式写入时，应使用兼容写入路径或明确只读，避免旧代码丢失核验。

正式切换前可恢复迁移前备份；正式产生新标注后若要回退数据库，必须先处理新增数据，不能直接用旧快照覆盖。迁移演练和备份要保留可核验的恢复报告。

## 15. 本轮计划的完成与开发启动条件

计划完成标准：本文件覆盖模型、接口、模块职责、设计模式取舍、入库/领取/核验事务、页面、测试、分阶段交付和迁移回退，并引用已核对的官方文档/开源实现。

启动开发前冻结产品默认值与 API 合同；若用户修改核验角色或分配规则，先更新对应章节和验收用例。后续实施以本计划为依据逐阶段交付。本次仅提交可审查的计划文档，不运行新功能导入、ASR 或生产迁移。
