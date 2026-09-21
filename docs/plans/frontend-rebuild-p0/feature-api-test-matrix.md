# P0 功能、API、迁移位置与验收矩阵

基线：`ef7696d`。表内测试均为现有测试；本次执行结果以 `regression-results.json` 为准。文件级映射表示该模块中相关测试，并不表示每项 UI 操作都有浏览器自动化。明确缺少覆盖的项目列在文末。

## 入口与导航兼容

| 入口/URL 状态 | 当前语义 | 迁移约束 |
| --- | --- | --- |
| `/`、`/index.html` | 需要标注员会话，恢复 assignment 或显示领取入口 | 服务端鉴权和 reason 跳转保留 |
| `/login.html?reason=...` | 登录、超时/被替换提示；已登录回首页 | query 原因不丢失，不被客户端路由吞掉 |
| `/completed.html` | 本人完成/跳过列表，包含本人的复标提交 | `include_submissions=1`、`version_id` 与去重口径保留 |
| `/completed.html?tab=cross-checks&round=...` | 兼容旧链接，转换为普通详情 | 转换后清理旧参数；不恢复专用标签 |
| `/admin`、`/admin/`、`/admin/login` | 独立管理员登录与控制台 | 独立 Cookie/CSRF，不复用标注员身份 |
| `/admin?view=...&range=...&from=...&to=...` | 管理视图、日期区间、直接访问 | reload/分享链接得到同一视图；日期影响范围不扩大 |
| admin 的 `annotator/q/status/...` | 人员、搜索、状态等筛选 | 以 `admin.js` URL 编解码及对应 API 为准保留 |
| admin 的 `view=cross-checks&state=...&round=...` | 全时段复核队列与具体轮次 | 与普通 date toolbar 隔离；深链接仍可打开 |
| `/static/*`、`/admin.css`、`/admin.js` | 已列举静态资源；管理端响应头更严格 | P1 新建受限构建资源路径，不能开放仓库任意文件 |
| `/api/*` | JSON/音频 API | 未知 API 不能返回 SPA HTML |

完整 Flask 路由含方法、源文件与行号见机器生成的 `route-inventory.json`，无需手抄所有兼容接口。

## 功能矩阵

测试路径均相对仓库根目录。高风险表示会影响任务、稿件、权限或敏感信息；不表示本次发现故障。

| ID | 当前入口/功能 | API 或本地协议 | 迁移位置 | 必须保留的验收 / 现有测试 |
| --- | --- | --- | --- | --- |
| F01 | 登录、续领 | `POST /api/login`; `GET /api/current-user` | `features/auth` | 刷新/退出再登录仍恢复同一任务；`tests/test_api.py::test_assignment_is_explicit_and_survives_logout` |
| F02 | 在线设备接管（高） | `POST /api/login/takeover` | `auth/SessionDialog` | 二次确认与 token 边界；旧设备冻结；`tests/browser/test_session_takeover.py::test_live_session_requires_takeover_and_freezes_old_page` |
| F03 | 失联设备自动登录 | 同 F01/F02 | `features/auth` | 不要求不必要的接管；`tests/browser/test_session_takeover.py::test_stale_presence_allows_automatic_login` |
| F04 | 会话心跳、活动与超时（高） | `POST /api/session/heartbeat`; 兼容 `GET /api/heartbeat` | `auth/session-service` | 心跳不无限延长 idle，活动不滑动绝对期限；`tests/browser/test_session_takeover.py`; `tests/test_api.py::test_get_heartbeat_is_presence_only_and_post_can_extend_idle` |
| F05 | 退出/停用/401 冻结（高） | `POST /api/logout`; 401/403 code | `auth/session-service` | 跳转前保存本地稿、冻结后禁写；`tests/browser/test_session_takeover.py::test_unauthorized_does_not_navigate_before_local_persist`; `test_account_deactivated_403_freezes_dirty_workspace`（同文件） |
| F06 | 登录公共统计、排行榜 | `GET /api/leaderboard` | `auth/PublicDashboard` | 时长格式、空/失败不阻塞登录；`tests/browser/test_login_dashboard.py`; `tests/test_leaderboard_api.py` |
| F07 | 28 日速度图、七日均线、场景过滤 | 同 F06，本地 Chart.js | `components/AnnotationSpeedChart` | 时区/自然日、场景切换、刷新失败保留已知图；`tests/browser/test_login_dashboard.py` |
| F08 | 领取范围、场景选择、可领取数量 | `GET /api/scenes`; `GET /api/assignment?source_scene=...` | `workspace/ClaimPanel` | claim_scenes 与 review_taxonomy 分离；`tests/test_api.py::test_scenes_api_exposes_review_taxonomy_separately_from_claim_scenes`; `tests/test_regression_reserved_claim_filters.py` |
| F09 | 原子领取/强制续领（高） | `POST /api/assignment/claim` | `workspace/assignment-service` | 单人/单任务约束、锁池反馈；`tests/test_scene_claims.py`; `tests/test_api.py::test_claim_reports_temporarily_locked_pool` |
| F10 | 普通/复标无感派发（高） | assignment 内部 `mode/round_id` | 同 F09 | 不显示身份、不泄漏原稿；`tests/browser/test_cross_check_workspace.py::test_cross_check_claim_is_blind_and_submits_awaiting_review`; `tests/test_cross_check_blind.py` |
| F11 | 任务标题与来源详情 | assignment/history/detail metadata | `components/TaskSummary/SourceDetails` | 当前和历史来源并存、选中证据为 headline；`tests/browser/test_regression_source_history_display.py`; `tests/browser/test_regression_english_reopen_display.py` |
| F12 | 文本/时间/质量分段编辑（高） | PATCH body `segments` | `workspace/editor` | 时间边界、质量排除、最新输入保留；`tests/test_api.py::test_save_complete_history_and_completed_api`; 浏览器音频/编辑细节见缺口 G02 |
| F13 | 波形与分段播放 | `GET /api/audio/<task_id>`; `/api/waveform/<task_id>` | `workspace/media` | 拖动、分段停止、焦点同步；API Range 鉴权由 `tests/test_api.py::test_audio_range_and_object_authorization` 覆盖，交互见 G02 |
| F14 | 字号调节、保存/完成快捷键 | localStorage、keydown | `workspace/editor` | 14–32px、偏好恢复、输入/模态框作用域；当前专项测试不足，见 G02 |
| F15 | 串行自动保存（高） | `PATCH/POST /api/assignment/current` | `workspace/persistence` | revision/lease/operation ID，单一在途写；`tests/test_repository.py`; `tests/browser/test_regression_review_save_queue.py` |
| F16 | 场景核验、混合/不确定/备注（高） | 保存 body `scene_review`; feature flags | `SceneReviewEditor` + 同 F15 | 可选性、标签验证、单独修改也保存；`tests/browser/test_scene_workflow.py`; `tests/browser/test_regression_review_only_save.py` |
| F17 | 离线本地稿、刷新恢复（高） | IndexedDB working_drafts | `workspace/persistence` | 同设备/同用户/同租约恢复，拒绝旧 revision；`tests/browser/test_session_takeover.py` |
| F18 | 请求重放与确认丢失（高） | IndexedDB outbox | 同 F17 | immutable body、同 ID、确认不清除新修改；`tests/browser/test_session_takeover.py`; `tests/browser/test_regression_offline_retry_backoff.py` |
| F19 | 冲突、复制/丢弃本地稿（高） | 409 + exportText | `SaveStatus/ConflictDialog` | 不静默覆盖，不导出 mode/round_id；`tests/browser/test_cross_check_workspace.py::test_save_error_and_exported_draft_do_not_reveal_task_mode` |
| F20 | 完成/跳过（高） | `POST /api/assignment/current/complete` | `workspace/submission-service` | 最新值原子终结、跳过原因、丢响应重放；`tests/browser/test_cross_check_workspace.py`; `tests/test_cross_check_submit.py` |
| F21 | 放弃/释放任务（高） | `POST /api/assignment/current/abandon` | 同 F20 | Task released 反馈、任务池/复核取消规则；`tests/browser/test_scene_workflow.py::test_scene_review_cleared_when_returning_to_claim_page`; `tests/browser/test_cross_check_workspace.py::test_task_abandon_uses_standard_copy` |
| F22 | Previous completed / Newer / Older | `GET /api/history/recent` | `workspace/history-viewer` | 只读，不释放当前任务；`tests/test_api.py::test_save_complete_history_and_completed_api`; `tests/browser/test_regression_source_history_display.py` |
| F23 | 本人完成记录、搜索/状态/加载更多 | `GET /api/completed?include_submissions=1` | `features/history` | 按音频本人最新记录去重，cursor/filter 一致；`tests/test_annotator_submission_history.py`; `tests/browser/test_cross_check_regressions.py::test_history_filter_switch_allows_reload_and_ignores_old_response` |
| F24 | 本人版本详情/音频（高） | `GET /api/completed/<task_id>?version_id=...` | `history/TaskDetailDrawer` | 只能访问本人许可版本；`tests/browser/test_cross_check_history.py`; `tests/test_api.py::test_completed_privacy_and_reopen_conflict` |
| F25 | 重新打开纠正（高） | `POST /api/completed/<task_id>/reopen` | `history` → `workspace` | 有未完成任务不能开启；草稿不影响 published；`tests/browser/test_regression_english_reopen_display.py`; `tests/test_scene_reviews.py::test_correction_draft_review_is_pending_not_published` |
| F26 | 旧历史链接与 flash | legacy query + sessionStorage | `history/compatibility` | 普通反馈、不透露复核裁定；`tests/browser/test_cross_check_history.py`; `tests/browser/test_cross_check_workspace.py::test_old_submission_flash_has_only_standard_feedback` |
| F27 | 管理员登录/退出/CSRF（高） | `/api/admin/login`, `/session`, `/logout` | `admin/auth` | 与标注员隔离、令牌校验、Cookie 属性；`tests/test_admin_api.py::test_admin_session_logout_expiry_and_csrf`; `test_admin_and_annotator_sessions_are_separate`（同文件） |
| F28 | Overview 库存与期间活动 | `/api/admin/overview`, `/timeseries` | `admin/overview` | 时区、存量与活动口径、来源去重；`tests/test_admin_api.py::test_admin_naive_shanghai_dates_are_interpreted_as_local_midnight`; `tests/test_regression_admin_metadata.py` |
| F29 | 人员目录与贡献详情 | `/api/admin/annotators`; `/<id>`; `/<id>/annotations` | `admin/annotators` | 当前贡献/历史提交/周转分别保留；`tests/test_admin_api.py::test_admin_overview_annotator_detail_and_keyset_pagination` |
| F30 | 场景领取权限（高） | `PUT /api/admin/annotators/<id>/scene-scope` | `admin/annotators/ScopeForm` | catalog/detail 未加载不可写；切人不被旧响应覆盖；`tests/browser/test_regression_scope_catalog_loading.py`; `test_regression_scope_editor_loading.py`; `test_regression_scope_save_navigation.py`（均同目录） |
| F31 | 全库任务、筛选与匹配总量 | `/api/admin/tasks`; `/metadata/facets` | `admin/corpus` | 筛选与计数同一谓词、cursor 不跨条件；`tests/browser/test_regression_corpus_date_stats.py`; `tests/test_regression_admin_metadata.py` |
| F32 | 管理任务详情/音频（高） | `/api/admin/annotations/<task_id>`; `/audio/<task_id>`; `/waveform/<task_id>` | `admin/TaskDetailDrawer` | 原始证据、版本、历史与管理员角色鉴权；`tests/test_admin_api.py`; `tests/test_regression_admin_metadata.py::test_task_detail_keeps_full_source_provenance` |
| F33 | 管理员修正场景核验（高） | `POST /api/admin/annotations/<task_id>/scene-review` | `admin/SceneReviewForm` | reason、版本和重放；`tests/test_scene_reviews.py::test_admin_review_correction_and_replay`; `tests/browser/test_scene_workflow.py` |
| F34 | Quality 信号与队列 | `GET /api/admin/quality` | `admin/quality` | 信号仅用于优先审阅，筛选与人员正确组合；`tests/test_admin_api.py::test_admin_quality_api_combines_queue_filters` |
| F35 | 单条/批量撤销（高） | `POST /api/admin/annotations/revoke/preview`; `/revoke` | `admin/actions/RevokeDialog` | preview、expected-version、幂等、批量二次密钥、全事务；`tests/test_admin_api.py::test_admin_revoke_preview_execute_replay_and_expected_version_conflict`; `test_admin_batch_revoke_is_atomic_via_api`; `test_batch_revoke_requires_step_up_key_but_single_revoke_does_not`（同文件） |
| F36 | 恢复撤销记录（高） | `POST /api/admin/annotations/restore` | `admin/actions` | 版本/资格与冲突保留；`tests/test_admin_repository.py`; `tests/test_cross_check_lifecycle.py` |
| F37 | 释放占用（高） | `POST /api/admin/assignments/<task_id>/release` | `admin/actions` | lease/版本与复核轮次生命周期；`tests/test_admin_repository.py`; `tests/test_cross_check_lifecycle.py` |
| F38 | 停用标注员（高） | `POST /api/admin/annotators/<id>/deactivate/preview`; `/deactivate` | `admin/actions/DeactivateDialog` | 预览、输入账号/原因/密钥、回收、冻结、保留审计；`tests/test_admin_api.py::test_admin_deactivate_requires_key_reauthentication_and_recycles_work` |
| F39 | 复核摘要、全时段队列 | `GET /api/admin/cross-checks` | `admin/cross-checks/Queue` | 不继承最近30天，状态和 deep link 正确；`tests/browser/test_cross_check_admin.py::test_overview_opens_all_time_awaiting_queue` |
| F40 | 抽样配置（高） | `GET/PUT /api/admin/cross-check-settings` | `admin/cross-checks/SettingsForm` | 百分比精确转 bps、版本冲突保留输入；`tests/browser/test_cross_check_admin.py::test_sampling_settings_percent_to_bps`; `tests/browser/test_cross_check_regressions.py::test_settings_conflict_preserves_inputs_until_explicit_resave` |
| F41 | 稿件对照、差异导航 | `GET /api/admin/cross-checks/<round_id>` | `admin/cross-checks/Review` | 按 code point，不自行 normalize；空差异率不当零；长稿/双向文本；`tests/browser/test_cross_check_diff.py` |
| F42 | 三种裁定（高） | `POST /api/admin/cross-checks/<round_id>/decision` | `admin/cross-checks/DecisionForm` | 原稿/复标/编辑稿 payload 分别正确；reason/确认/幂等/409；`tests/browser/test_cross_check_admin.py`; `tests/browser/test_cross_check_regressions.py` |
| F43 | 取消进行中复核（高） | `POST /api/admin/cross-checks/<round_id>/cancel` | `admin/cross-checks/CancelForm` | awaiting 状态不可伪装为取消；`tests/browser/test_cross_check_admin.py::test_cancel_in_progress_and_not_awaiting` |
| F44 | 审计列表 | `GET /api/admin/audit` | `admin/audit` | 日期、cursor、动作结果与历史保留；`tests/test_admin_api.py::test_admin_audit_api_records_revoke` |
| F45 | 人员/审计导出可见 CSV | 已加载行 → 浏览器 Blob/下载 | `admin/export-visible` | 导出范围和转义；未找到专项浏览器测试，见 G03 |
| F46 | 英文 UI 与阿拉伯语内容 | 英文标签、局部 RTL | 公共组件和各业务页 | 不用中文/技术状态替代既有文案；`tests/browser/test_english_platform_acceptance.py`; `tests/browser/test_cross_check_diff.py` |
| F47 | 功能开关兼容 | scenes/metadata `features` | `api/types` + 对应 feature | metadata UI 或写入关闭仍可标注；`tests/test_compatibility_flags.py`; `tests/browser/test_scene_workflow.py::test_metadata_ui_off_still_saves_transcript` |
| F48 | 内容转义与链接 | 来源/文本/错误渲染 | 公共格式化与详情组件 | 来源和 diff 中的恶意字符串按文本；`tests/browser/test_cross_check_diff.py::test_xss_sample_is_plain_text`; `tests/browser/test_scene_workflow.py` |
| F49 | 页面、资源与音频响应 | 服务端 page guards、CSP、Range | P1 Flask 构建接入 | 旧 URL、未登录拒绝、未知资源404；`tests/test_api.py::test_pages_require_login_and_completed_route`; `test_audio_range_and_object_authorization`（同文件）；新构建资源覆盖见 G05 |
| F50 | 客户端报错 | `POST /api/clientlog` | 公共错误边界/请求层 | 保留可诊断性，不把原始敏感 payload 展示给用户；现有 API 测试和源码契约，React 错误边界需新增验收 |

## 已识别的测试缺口

| ID | 当前证据范围 | 后续需补充 |
| --- | --- | --- |
| G01 | 部分测试保存截图和检查页面无溢出；没有统一的视觉差异门禁 | P1 样例固定字体/视口，P2 起比对关键布局与图片；保留人工复核 |
| G02 | 基本转写与 API Range 被测；未找到完整覆盖分段拖动、播放停止、字号、IME、快捷键作用域的专项浏览器组合 | P1/P4 加音频联动、阿拉伯语输入/光标、键盘与焦点验收 |
| G03 | 导出按钮源码支持 CSV；暂无专门的下载内容浏览器验证 | P3 验证可见范围、列内容、引号/换行转义、文件可打开 |
| G04 | 旧版本地草稿有完整竞态测试；还没有 React 版 | P4 补旧↔新存储互通、remount 单一保存队列、存储失败 |
| G05 | 现有 page guards/CSP/API 有覆盖；未有构建资源/懒加载/缓存发布模型 | P1/P5 补 hash 静态资源、深链接、404、旧标签页、Nginx Range |
| G06 | UI 自动化主要运行 Chromium | P1 确定浏览器支持范围；其他浏览器不因 Chromium 通过而声称已验收 |
| G07 | 当前没有业务库 DSN | 分段 p50/p95/p99/max 暂未实测；只读统计脚本已交付，不能用合成夹具代表真实分布 |

## 高风险迁移顺序

先保留 API 与 payload 语义，再迁移保存/会话服务，再接入 React 编辑器。管理端按只读视图与写操作分阶段；F15–F21、F25、F30、F35–F43 必须以现有回归为底线。没有覆盖的视觉/交互项目仍须有明确验收，不因测试矩阵通过而自动视为完成。
