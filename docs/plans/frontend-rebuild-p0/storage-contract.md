# P0 浏览器存储与保存兼容契约

基线：`ef7696d`。本文件记录旧版实际结构；它是 P1/P4 的输入，不是新 schema 设计。

## 1. 存储目录

| 位置 | 名称 | 实际用途 | 迁移要求 |
| --- | --- | --- | --- |
| IndexedDB | `annotation-offline-v1`，数据库版本 `1` | 工作稿与尚未确认的请求 | 第一版继续可读写；升级前验证旧→新→旧 |
| Object store | `working_drafts` | 当前用户、任务的整份工作快照 | out-of-line key，无 keyPath、无自增、无索引 |
| Object store | `outbox` | 不可变请求，等待确认/重放 | out-of-line key，无 keyPath、无自增、无索引 |
| localStorage | `annotator.transcriptFontSize` | 阿拉伯语编辑字号 | 字符串数值；默认 18、范围 14–32、步长 2；只影响偏好 |
| sessionStorage | `annotator.taskFlash` | 完成/跳过/释放后的普通反馈 | 消费后删除；只保留 kind/status 等普通任务语义 |
| sessionStorage | `annotator.crossCheckFlash` | 旧版兼容提示 | 读取 fallback，消费时与新 key 一并移除；不复活质检提示 |
| sessionStorage | `annotation.admin.csrf` | 当前 admin CSRF token | 保留现有 session 校验和清理；不是管理员密钥 |
| HttpOnly Cookie | 标注员 Session、独立管理员 Session | 身份和权限 | 名称/安全属性以服务端配置为准，React 不读取或自造替代 token |

证据：`static/offline-drafts.js:4–15,47–71`，`index.html:231,267–285,354–365`，`static/annotator-feedback.js:4–33`，`admin.js:4,153–165`，`server.py` 的 session/admin Cookie 配置与登录响应。

Cookie 值、admin CSRF 值和真实用户数据不写进交付件。采集的 IndexedDB 样例全部来自本次一次性数据库和合成用户，任务/lease/operation UUID 也是临时测试值。

## 2. working_drafts 的记录形状

主键：`String(username) + ":" + String(task_id)`，不是 UUID task 单独作 key。

| 字段 | 旧版来源/语义 | 必须保留的行为 |
| --- | --- | --- |
| `schema_version` | `saveWorkingDraft` 默认写 `1` | 不静默当作新结构 |
| `updated_at` | ISO 时间字符串 | 清理过期记录的依据之一 |
| `status` | `dirty` / `clean` | 本地状态，不等于服务器已成功终结任务 |
| `username` | 当前账号 | 用户隔离 |
| `task_id` | assignment 的任务 | 任务隔离 |
| `version_id` | 当前版本 | 恢复时识别旧草稿版本 |
| `lease_token` | 当前 assignment lease | 不把旧租约草稿覆盖给新 assignment |
| `server_revision` | 最近确认的服务端 revision | 恢复/409 检查；不能在发送时提前推进 |
| `segments` | 当前全部分段快照 | 保留 text/asr_text、时间、质量标记与已有字段 |
| `scene_review` | 当前场景核验值 | 与文本同一保存链；可能为 null |
| `dirty_segment_ids` | dirty 与在途快照的分段 ID 合集 | 不以屏幕上已渲染行代替完整集合 |
| `review_dirty` | 未确认的场景核验修改 | 单独修改核验也能保存 |
| `mode` | 普通/复标内部模式 | 只用于恢复与协议，不显示给标注员 |
| `round_id` | 复核轮次，普通任务为空 | 同上；导出给用户时去除 |

`saveWorkingDraft` 是整条 `put`。源代码允许传入对象覆盖默认字段，因此迁移验证应检查已有记录，不把默认值误写成后端强校验。

## 3. outbox 的记录形状

主键：`operation_id`。`putOutbox` 的固定字段：

```text
operation_id, username, task_id, route, method, body, created_at, attempts
```

- `body` 是发送时冻结的请求体。保存请求通常包含 `lease_token`、`expected_revision`、`operation_id`、本次 `segments`；必要时有 `scene_review`。
- 完成/跳过请求体另含 `target_status`、`skip_reasons` 等终结字段；释放任务有自己的路由和 body。复放不能全部当作 PATCH 保存。
- 相同 operation ID 的 username/task/route/method/body 必须一致；如果用户在等待期间继续输入，需要下一次 operation，不能修改在途 body。
- 用户队列按 `created_at` 排序。`attempts` 记录重试，不能换 ID 后归零来伪装新请求。
- `confirmOutbox` 在同时包含 outbox/draft 的一个 IndexedDB 事务里删除已确认请求并更新对应草稿 revision。
- 清理 dirty 只针对“当前本地值仍等于已发送值”的分段/核验；在途期间新输入必须继续 dirty。
- `deleteTaskData` 清理指定用户/任务的草稿与队列；不能清理其他账号的数据。
- 过期清理使用配置天数（缺省 7 天），按 `updated_at` 或 `created_at`，并可限定用户。
- `AnnotationOffline.exportText` 移除 `mode` 与 `round_id` 后序列化，保留标注员无感质检。

## 4. 实测证据与测试映射

采集脚本：`scripts/frontend_p0_capture.py::test_capture_offline_conflict_and_takeover`。

- `storage-offline-sample.json`：断网后实际编辑、手动保存失败，从浏览器 IndexedDB 读出 schema、keys 与记录。
- `storage-recovered-sample.json`：同一浏览器重连且服务器确认后再次读取，可对照 outbox 清空与草稿状态。
- `capture-persistence.json`：故意制造离线与 409 的截图记录；其中网络报错属于有意注入，不当作自然故障计数。

现有业务回归需保留：

| 契约 | 现有测试节点（位于 tests/browser） |
| --- | --- |
| 本设备恢复，不同步到其他设备 | `test_session_takeover.py::test_offline_draft_restores_in_same_context_not_other_device` |
| 旧请求确认不能清除更新输入 | `test_session_takeover.py::test_online_event_replays_save_and_preserves_newer_offline_edit` |
| 响应丢失后刷新 | `test_session_takeover.py::test_response_lost_then_reload_does_not_ack_newer_local_edit` |
| 版本不符需复制/明确丢弃 | `test_session_takeover.py::test_revision_mismatch_requires_copy_or_explicit_discard` |
| 重连重放完成请求 | `test_session_takeover.py::test_online_event_replays_pending_completion` |
| 保存原 body 后再保存新核验 | `test_regression_review_save_queue.py::test_lost_save_response_retries_identical_body_then_saves_new_review` |
| 断网退避 | `test_regression_offline_retry_backoff.py::test_persistent_offline_save_uses_bounded_retry_backoff` |
| 导出稿件不暴露模式 | `test_cross_check_workspace.py::test_save_error_and_exported_draft_do_not_reveal_task_mode` |

## 5. P4 还必须增加的迁移验收

现有回归证明旧版本身的行为，尚不能证明 React 与旧版互通。新增版本必须用本文件的 shape 与隔离浏览器完成：

1. 旧版写 dirty 稿件及待确认 PATCH，新版刷新恢复并确认；继续输入不丢字。
2. 新版写同样结构，切回旧版能恢复；终结请求在两种页面间只生效一次。
3. 两用户、两任务、旧 version/lease/revision 的交叉恢复均正确隔离。
4. 用户可见导出继续过滤内部模式；旧 flash 与字体偏好仍兼容。
5. 存储不可用/额度不足/旧记录损坏，给出真实错误状态，不显示假“已保存”。
6. React StrictMode 与路由 remount 后仍只有一个写入队列和一套心跳；草稿服务不能随视觉组件卸载而丢状态。
