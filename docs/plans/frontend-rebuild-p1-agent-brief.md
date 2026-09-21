# P1 实现 agent 任务书

下面的内容可以直接作为实现任务。详细契约以 [P1 实现方案](2026-09-21-frontend-rebuild-p1-implementation.md) 为准。本文件是待执行要求，不是已完成记录。

## 可复制的任务描述

请在 Egyptian Arabic annotation tool 仓库实施前端重建 **P1：底座与代表页**，完成代码、测试和验收材料。

工作起点是远端 `codex/frontend-rebuild`，必须包含已提交的 P0：`cd6aa850dc97b32218fa8a42363b7a284d23fdc9`。独立分支可命名为 `codex/frontend-p1`。先检查当前工作区，保留其他人的改动；不要重新从缺少 P0 的 main 开始。若当前 checkout 尚无本任务书/详细方案，先取得这两份文件，不根据文件名自行猜测内容。

先完整阅读：

1. `docs/plans/2026-09-21-frontend-rebuild-p1-implementation.md`。
2. `docs/plans/frontend-rebuild-p0/README.md`、`issues.md`、`feature-api-test-matrix.md`。
3. `docs/plans/frontend-rebuild-p0/storage-contract.md`、`performance.md`。
4. `docs/plans/2026-09-21-frontend-rebuild-plan.md` 的业务边界。

按详细方案 A–H 的顺序实施：

- 固定 Node/npm/依赖，建立 Vite + React + TypeScript strict 工程。
- 首选 Ant Design 6 + CSS Modules，优先静态样式；先验证实际 Flask CSP 下的两主题、弹层、表格，再铺页面。只有验证失败才按方案替换唯一主库。
- 实现默认关闭的预览开关、受限 hash 资源路径、构建预检、MIME/缓存/404。保留旧正式入口。
- 实现共享 tokens、两个角色 Shell、FilterBar、AsyncState、CursorPager、SaveStatus 等实际需要的组合。
- 管理代表页接 `/api/admin/tasks` 和 facets，验证筛选/日期/cursor/匹配数。使用真实 API + 隔离数据库夹具。
- 工作台代表页接当前 assignment，支持长阿拉伯语、基本音频联动、真实编辑与保存。笔记本首屏可见正文；390px 直接可编辑。
- 独立保存服务验证单一在途请求、不可变重试、A 在途继续输入 B、scene-review-only、离线、401/403/409、StrictMode、旧↔新普通 PATCH 草稿/outbox 兼容。
- 接入 CI，保留 PostgreSQL 16/18 完整矩阵，新增 P1 集成测试；生成截图、几何测量、三档性能和验收报告。

执行中必须遵守：

- 通用控件优先组件库；不引入第二套 UI、整套后台模板或大量手工弹窗/筛选器。
- API、数据库、音频鉴权/Range、管理员 Cookie/CSRF、盲标边界保持；不读取生产凭据来填测试数据。
- 不放宽为 `unsafe-inline` / `unsafe-eval`，不禁用 CSP；nonce 如需要只对新 HTML 精确适配并验证。
- 正式登录/历史/六个管理页/管理写操作/完成跳过释放的全量迁移留后续阶段，不放无实现按钮冒充完成。
- 保持 `annotation-offline-v1` schema 和字段语义；未支持的旧终结 outbox 必须保留并回旧页面处理。
- 不删除旧测试、弱化竞态/权限断言或以缺 dist 为由 skip 新测试；P0 证据不覆盖。
- UI 使用英文，阿拉伯语正文局部 RTL。无渐变大标题、装饰卡片堆叠或字符拼图标。

验收以详细方案第 9、10 节为准。测试必须区分旧版基线和本次新版本；P0 的 503 passed / 1 skipped 不能直接算成本次通过。记录未取得真实语料分布、未执行的浏览器/部署检查，不能伪造完成状态。

交付包括可运行代码、`frontend/README.md`、按工作包拆分的提交，以及 `docs/plans/frontend-rebuild-p1/` 内的组件/CSP决策、截图、性能原始数据、测试结果、验收表和 P2 交接。普通工程选择按方案继续完成，不反复询问；遇到确实阻碍完成的兼容性问题，给出复现、影响及已尝试方案。

最终回复说明：实际完成范围、运行命令与入口、测试结果、commit、未通过或带条件通过项。推送/PR按本次实现任务的明确授权执行，不进行生产部署。

## 交接时附带的文件

远端目前已有 P0；P1 方案与本任务书是之后生成的文档。让其他 agent 接手时，需要把这两份文件放入其工作区，或提供包含它们的后续文档提交。仅提供 P0 commit 不足以取得 P1 实施要求。
