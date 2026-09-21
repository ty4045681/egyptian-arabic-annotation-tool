# 前端重建 P1 实现方案：底座、组件规范与代表页

日期：2026-09-21。状态：**待实现的交接方案，不是实施完成报告**。

基础分支：`codex/frontend-rebuild`。P0 提交：`cd6aa850dc97b32218fa8a42363b7a284d23fdc9`，已推送远端。业务源码基线：`ef7696d508608725069bcfca099e498fc8fc6885`。

P1 交付一个可通过 Flask 运行的 React 前端底座，确定唯一组件库，并用**真实 API 的管理任务列表**和**可编辑、可保存、可播放音频的工作台代表页**证明布局与技术路线可用。新版通过默认关闭的预览入口验收；正式页面的切换留在后续阶段。

配套：[可直接交给实现 agent 的任务书](frontend-rebuild-p1-agent-brief.md)。总计划：[前端重建计划](2026-09-21-frontend-rebuild-plan.md)。P0：[交付入口](frontend-rebuild-p0/README.md)。本方案细化总计划的 P1；总计划中 P2–P5 的业务范围继续有效。

## 1. 开始前与范围

### 1.1 基线与必读资料

实现应从包含 P0 提交的分支继续，不再从缺少 P0 的 `main` 重建。独立工作目录可执行：

```bash
git fetch origin
git switch -c codex/frontend-p1 origin/codex/frontend-rebuild
git merge-base --is-ancestor cd6aa850dc97b32218fa8a42363b7a284d23fdc9 HEAD
```

已有同名工作分支时继续使用，先检查 Git 状态；保留其他人的未提交文件。当前仓库的 `session-ses_f3ca.md` 不属于前端交付。

按以下顺序读，避免凭截图推测接口：

1. [P0 问题清单](frontend-rebuild-p0/issues.md)、[截图画廊](frontend-rebuild-p0/gallery.html)：U01–U10 的证据和视觉问题。
2. [功能/API/测试矩阵](frontend-rebuild-p0/feature-api-test-matrix.md)：F01–F50，以及 G01–G07 的覆盖缺口。
3. [存储契约](frontend-rebuild-p0/storage-contract.md)、[性能基线](frontend-rebuild-p0/performance.md)。
4. `server.py` 的页面路由、`secure_admin_responses`、`admin_query_filters`、assignment/session API；`annotation_metadata/routes.py` 的静态文件白名单。
5. `admin.js` 的 `commonQuery`、`restoreUrlState`、`loadCorpus`；`annotation_repository.py::admin_tasks` 的实际响应字段。
6. `index.html` 的保存逻辑、`static/offline-drafts.js`、`static/annotator-session.js`，以及本方案第 9 节指定的测试。

P0 全量结果为 503 passed / 1 skipped，采集场景 7 passed；这是旧版基线，不能作为 P1 的测试结果复用。真实语料分段分布仍未取得，不阻塞底座和合成规模验证。

### 1.2 P1 必须交付

| 编号 | 交付项 | 可检查的结果 |
| --- | --- | --- |
| D1 | Vite / React / TypeScript 工程 | 版本与 lockfile 固定，可重复安装、检查、构建 |
| D2 | Flask 静态产物接入 | 明确入口、资源路径、缓存、404、鉴权、开关和构建预检 |
| D3 | 组件库/CSP 决策 | 两种主题和主要控件在真实响应头下通过；附决策记录和证据 |
| D4 | 统一 tokens 与共享布局 | 管理浅色、标注深色，控件、间距、错误和空状态一致 |
| D5 | 管理任务列表代表页 | 实际 API、筛选、日期、cursor、匹配总量、加载/空/错误状态 |
| D6 | 工作台代表页 | 长阿拉伯语输入、基本音频联动、局部 RTL、字号、首屏/窄屏布局 |
| D7 | 最小保存服务验证 | 真实 PATCH、IndexedDB 兼容、不可变重试、在途输入、冻结及 remount |
| D8 | 自动检查与验收报告 | CI 接入、截图/几何断言、性能原始数据、命令及未完成事项 |

**P1 不包含**：六个管理页全量迁移、管理写操作、完整登录/接管 UI、历史/纠正迁移、完成/跳过/释放迁移、数据库变更、生产部署及流量切换。代表页不放置无实现的操作按钮；未迁移功能通过完整导航回旧页面。

P1 中的“真实 API”指运行现有 Flask 和一次性 PostgreSQL，由测试准备合成账号、任务、音频后走实际接口。验收不需要生产账号、真实音频或线上数据库。

### 1.3 P0 问题如何进入 P1

| P0 项 | P1 验证 | 完成边界 |
| --- | --- | --- |
| U01：复核筛选使页面宽达 2114px | FilterBar 用长标签/多选/日期测试；代表列表页面无横向溢出 | 原复核业务页在 P2 迁移后才能标为已修复 |
| U02：1366×768 下首个正文约 y=928px | 首屏显示任务、保存状态、播放入口及至少一行完整可编辑正文 | 新代表页达标 |
| U03：390px 下正文从 x≈506px 开始 | 窄屏将分段元数据放在正文上方，直接编辑，无需先横滚 | 新代表页达标 |
| U04–U08：长文件名、布局与视觉不一致 | 限制布局伸展，统一 tokens、图标、标签和操作层级 | 公共组件和两页达标；旧页不批量改样式 |
| U09：离线报错产生未处理 rejection | 新请求层/日志函数显式处理失败，不递归上报自己的失败 | 新入口通过；旧版问题保留记录 |
| U10：1000 分段约 1.7 万 DOM、输入 p95≈91ms | 行状态和媒体隔离，100/500/1000 三档复测 | 给出方案结论，不宣称已覆盖真实最大任务 |

## 2. 技术决策与依赖边界

采用 **Vite + React + TypeScript strict + Ant Design + CSS Modules**。仅在第 5 节的兼容性验证失败后执行已定义的替代流程，不在各页面混用主组件库。

| 项目 | P1 决策 |
| --- | --- |
| Node / npm | 使用 Node 24 LTS；实施时锁定实际 patch 和 npm 版本，写入 `.nvmrc`、`package.json` 与 CI |
| React / Vite / TS | 选择相互兼容的稳定版本，提交精确直接依赖及 `package-lock.json`；CI 使用 `npm ci` |
| UI | Ant Design 6.x 的稳定版本与 `@ant-design/icons`；需要 zeroRuntime 时不降到 5.x |
| 样式 | CSS Modules 管业务布局，统一主题源生成颜色/尺寸变量；不再叠加 Tailwind |
| 路由 | React Router 客户端模式，路由懒加载；现有 `.html` 与 admin query 语义为后续正式迁移保留 |
| 服务端查询 | TanStack Query 管理列表/session 读取；不管理保存队列，不持久化身份缓存 |
| 表单 | Ant Design Form；编辑器分段状态独立，不将上千行全部放入一个 Form |
| 状态与持久化 | 小型独立 store / controller，通过明确订阅接入 React；P1 不引入 Zustand |
| 音频 | 原生 `HTMLAudioElement` + 独立 Canvas，保持现有音频 API |
| 日期 | DatePicker 所需日期库显式声明；复用现有 Asia/Shanghai 与日期边界语义 |
| 测试 | 沿用 Python pytest/Playwright；Vitest 仅用于查询参数与保存状态等纯逻辑 |
| 暂缓依赖 | React Hook Form、TanStack Table、另一套 UI、整套后台模板、Service Worker、新音频引擎 |

Node 24 当前处于 LTS；Vite 文档的最低版本要求不等于适合新项目的长期运行版本。[Node 发布表](https://nodejs.org/en/about/previous-releases)、[Vite 环境要求](https://vite.dev/guide/)。实施 agent 在首次安装时记录实际选定版本；本文不假装已经完成依赖兼容验证。

建议目录如下，只创建本阶段实际使用的模块：

```text
frontend/
  .nvmrc
  package.json / package-lock.json
  vite.config.ts / tsconfig*.json / eslint.config.*
  index.html
  scripts/                      # 静态主题生成、产物检查
  src/
    app/                        # 入口、providers、路由、错误边界
    theme/                      # 共享 token 源、admin / annotator 配置
    api/                        # client、API 类型、query keys、错误映射
    components/                 # 两个 Shell、PageHeader、FilterBar 等
    features/admin/corpus/      # 真实任务列表代表页
    features/workspace/
      editor/                   # 行编辑、场景核验最小输入
      media/                    # audio ref、Canvas、时间订阅
      persistence/              # controller、offline adapter、串行队列
    features/auth/              # session 服务及错误处理
    dev/                        # 组件状态矩阵，仅 P1 验证构建包含
    test/                       # 必要纯逻辑测试
  dist/                         # 常规生产构建，不提交
  dist-p1/                      # P1 验证构建，不部署、不提交
frontend_delivery.py             # 新增：构建检查、受限资源和页面响应 helper
tests/test_frontend_delivery.py  # 新增：服务端构建/路由/响应头测试
tests/browser/test_frontend_p1_*.py
docs/plans/frontend-rebuild-p1/  # 实施后新增的验收证据，不覆盖 P0
```

文件名是交接约定，可按实现小幅调整，并同步文档；目录不能成为把旧 `index.html` 整段脚本搬进一个 React 组件的理由。

## 3. 实施顺序与提交拆分

顺序为 A → B → C → D → E/F → G → H。E/F 在接口稳定后可分别完成；本任务书不要求启动多个 agent。

| 工作包 | 实现内容 | 依赖与完成条件 | 建议提交 |
| --- | --- | --- | --- |
| A | 工程初始化、版本锁定、命令和忽略规则 | 可 `npm ci`、typecheck、lint、build | `build: scaffold frontend workspace` |
| B | 产物 helper、预览开关、精确路由、缓存/404/预检 | Flask 能加载真实构建；旧入口原样工作 | `feat: serve gated frontend preview builds` |
| C | Ant Design/CSP 最小样例及决策 | 第 5 节整套验证通过后才能批量写 UI | `feat: validate frontend components under CSP` |
| D | 主题、两个 Shell、共享布局和状态组件 | 两主题/五种尺寸/弹层样例通过 | `feat: add shared frontend layout and themes` |
| E | 只读 Corpus 代表页 | 实际 API、日期/cursor、错误及迟到响应验收 | `feat: add corpus preview with real API data` |
| F | 工作台与保存/媒体最小验证 | 第 8 节场景通过；真实本地保存和 PATCH | `feat: validate workspace editing and persistence` |
| G | CI、完整回归、截图与性能对比 | 第 9 节必需检查全部有结果 | `test: verify frontend P1 integration` |
| H | 运行说明、组件决策、验收证据与 P2 交接 | 第 10 节交付清单齐全 | `docs: record frontend P1 acceptance` |

总计划的 2–3 个工程日是粗估；按本方案明确后的保存互通、构建接入和验收范围，建议按 **3–5 个工程日**安排。CSP 和保存验证是主要不确定项，C 完成后据实重估，不能为维持估算删除失败场景。完成全部工作包后再报告 P1 完成。

## 4. 构建、Flask 接入与预览隔离

### 4.1 必须提供的命令

`frontend/package.json` 提供 `dev`、`typecheck`、`lint`、`test`（一次性运行）、`build`、`build:p1`、`check:dist`。`frontend/README.md` 记录安装、同源 API 开发代理、Flask 运行和验收命令。

- `build`：正式生产构建，产物 `dist/`，不包含 `src/dev` 的组件样例和测试夹具。
- `build:p1`：仍使用生产优化的 Vite build，仅增加组件验证入口，产物 `dist-p1/`；不是 Vite dev server，也不是可上线发布包。
- `check:dist`：默认检查两个输出目录；也可用参数指定其中一个。验证 HTML、manifest、递归引用的 chunks/CSS/资产存在，输出版本/校验清单。
- `.gitignore` 增加 `frontend/node_modules/`、两个 dist、测试临时产物和生成的主题中间文件；lockfile、主题生成代码及精简验收证据提交。

Vite 支持后端读取构建 manifest；本项目使用 Vite 构建 HTML 与 manifest，Flask 返回 HTML，manifest 用于产物完整性和资源清单校验。[Vite 后端集成](https://vite.dev/guide/backend-integration)。

### 4.2 具体入口约定

新增服务端配置 `ANNOTATION_FRONTEND_PREVIEW=false` 与 `ANNOTATION_FRONTEND_BUILD=standard|p1`，在现有应用配置加载处统一解析。非法值启动报错；测试通过 `app.config` monkeypatch，不靠重复导入全局 Flask app。`p1` 只允许预览开关开启时使用。

| 路径 | P1 行为 |
| --- | --- |
| `/`、`/index.html`、`/login.html`、`/completed.html` | 继续旧页面、原 session guard 和 reason 跳转 |
| `/admin`、`/admin/`、`/admin/login` | 继续旧管理入口，保持所有现有 query 和写操作可用 |
| `/admin/preview?view=corpus` | 新 Corpus 代表页；需预览开关和有效管理员会话，未登录转旧 `/admin/login` |
| `/frontend-preview/workspace` | 新工作台代表页；需预览开关和有效标注员会话，沿用原 reason 跳转 |
| `/frontend-preview/components` | 两主题状态矩阵；需预览开关、`p1` 构建与管理员会话 |
| `/frontend/assets/<path:filename>` | 只提供当前选定构建的公开 hash 资产 |
| 其他路径、未知 `/api/*`、未知资源 | 保持 404/原 API 错误；不返回 SPA HTML 兜底 |

上述预览 HTML 在开关关闭时均返回 404；常规构建即使打开预览也不提供组件样例。前端路由必须懒加载样例，并以构建常量裁剪；不能只隐藏导航菜单。测试正常构建中不存在样例入口和 fixture 数据。

管理代表页只支持 `view=corpus`；无 view 可规范化到 corpus，其他 view 明确拒绝或完整跳转相应旧 `/admin?view=...`，不能显示同一个列表冒充其他管理页。跨旧/新入口使用完整导航，不能由 React Router 接管旧页面。

P1 不提前开启总计划中的管理端/标注端正式切换开关：在业务迁移完整前，不允许把整个 `/admin` 或 `/` 指向仅含代表页的应用。

### 4.3 资源、缓存与预检

1. Vite 固定同源资源前缀 `/frontend/`，hash 文件位于 `assets/`；HTML 使用绝对资源 URL，直接访问嵌套预览路径和刷新都正常。
2. Flask 只在当前选定的 `dist/assets` 或 `dist-p1/assets` 根目录内提供构建资产，使用安全路径处理，并校验产物清单。拒绝目录穿越、隐藏文件、manifest、source map、HTML 和仓库源文件；不要扩大 `/static/` 白名单。
3. `check:dist` 收录包括动态 import 在内的资产；不能把只验证主 JS 当作完整性检查。404 不缓存为 immutable；有效 hash 资产 `public, max-age=31536000, immutable`，MIME 正确并加 `nosniff`。
4. 所有新 HTML 为 `no-store`；新工作台/样例页也应用与管理页等价的 CSP/安全头。不能只依赖现有 `secure_admin_responses` 的 `/admin` 前缀匹配，因为它不会覆盖 `/frontend-preview/*`。统一响应头生成处，并测试 `/admin/preview` 的 nonce 策略不会被原 after_request 再次覆盖。
5. 预览关闭时缺少 dist 不影响旧系统启动。预览开启时启动预检要求选定构建完整；测试环境中的后续缺失必须明确失败，不静默回旧版、不把资源错误响应成 HTML。
6. P1 的 Nginx 继续通过现有 `location /` 代理 Flask，hash 缓存头由 Flask 给出。暂不增加容易出错的 Nginx alias。音频 `/_protected_audio/` 的 internal 与鉴权链保持。
7. 验证构建无线上 Node 服务、CDN、远程字体依赖。记录当前构建提交、Node/npm 版本、lockfile hash、资产大小。

旧标签页跨 release 懒加载、保留上一版资产及真实 Nginx 部署回滚是 P5 的完整验收项。P1 要完成产物缺失/404/缓存语义和预览关闭回退；不得据此宣称已做生产回滚演练。

## 5. CSP 与组件库决策门槛

当前管理端策略为 `script-src 'self'; style-src 'self'`，另有 `default-src 'self'`、媒体/连接同源等约束。P1 必须在 **Flask 返回的生产构建**上验收，保留原管理页策略。

### 5.1 验证顺序

1. 先试 Ant Design 6 的 `zeroRuntime` 与本地静态 CSS。官方说明该模式从 6.0.0 提供；全量预置 CSS 不能直接证明自定义两主题都正确。[Ant Design 主题文档](https://ant.design/docs/react/customize-theme/)。
2. 将 admin/annotator 配置放在同一主题源；使用构建期样式生成验证两主题，并确保主题 class/变量与弹层容器一致。按实际安装版本核对提取库 API，不照抄不同版本示例。[官方静态提取工具](https://github.com/ant-design/static-style-extract)。
3. 验证 Select、DatePicker、Tooltip、Modal、Drawer、消息提示、Table 滚动/固定列、Input.TextArea、Slider，以及按钮交互。首次触发、滚动、resize、关闭后重新打开都要检查。
4. 静态方案仍有必要的受控样式注入时，允许仅对新 HTML 增加每响应随机 nonce。由 Flask 生成，HTML meta 安全传递给样式 provider/ConfigProvider；CSP 与实际 style 元素使用同一值，响应不缓存。[ConfigProvider CSP 配置](https://ant.design/components/config-provider/)。
5. 不添加宽泛 `unsafe-inline` / `unsafe-eval`，不关闭浏览器 CSP，也不把违规日志过滤掉。nonce 不能自动解决元素 `style` 属性问题；DOM 中存在 style 属性也不自动证明被 CSP 阻止，应以实际写入方式、违规事件及布局结果判断。[MDN style-src-attr](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/style-src-attr)。

组件目录样例同时保留紧凑布局、长错误、主题弹层与滚动场景；公共提示使用 provider 上下文内的 API，避免另建根节点导致主题/nonce 丢失。

### 5.2 通过与失败处理

- 必须通过：两主题均可操作，无 CSP 违规、JS 未处理异常、图标失样式、弹层偏位、焦点遮挡和外部资源请求。收集 `securitypolicyviolation`、console、pageerror、实际 CSP header 和截图。
- 仅头部存在 CSP 不算通过；仅未报错但日期/下拉无法操作也不算通过。
- 决策探索以约半个工程日为初始时间盒。若无法达标，输出最小复现和失败组件，停止向该方案铺更多页面。
- 可执行总计划已给出的 **shadcn/ui + 构建 CSS** 备选；这属于唯一主库替换，要更新主题/目录/依赖与本任务验收，不保留 Ant Design 与另一套控件混用。备选组件也必须跑同样的弹层/CSP 验证，不能预设一定兼容。
- 若备选也无法通过，报告具体阻碍和已完成证据；不得以降级 CSP 或仅提供静态截图标记 P1 完成。

最终写 `docs/plans/frontend-rebuild-p1/component-decision.md`：实际版本、选中方案、尝试及原因、最终响应头、剩余限制。后续 agent 无需重新进行无依据的库选型。

## 6. 主题与共享组件实现约束

### 6.1 统一 token

| 类别 | 初始值/规则 |
| --- | --- |
| 密度 | UI 正文 14px，辅助文字不低于 12px；转写 18px，保留 14–32px 偏好 |
| 间距 | 4 / 8 / 12 / 16 / 24 / 32px；page gutter 桌面 24、小屏 16 |
| 控件 | 默认 32px；窄屏主要触控操作 44px；同排控件同尺寸 |
| 圆角 | 控件 6px、容器 8px |
| 颜色 | Ant Design 默认语义色为起点，两主题映射同一语义 token；普通文字对比度目标 4.5:1 |
| 字体 | 系统字体/许可明确的本地字体；测试固定实际字体环境；数字 tabular-nums |
| 表单 | 标签在上，输入底线对齐；错误空间及换行规则稳定 |
| 装饰 | 无渐变标题、发光、大面积玻璃效果、无意义指标卡；SVG 图标统一来源 |

token 必须由一份可维护源同时服务 Ant Design 配置与业务 CSS。两页不能各自复制一份颜色表。P1 没有用户主题切换需求；主题由角色入口选择。

### 6.2 必须完成的共享组合

| 组件 | 责任与限制 |
| --- | --- |
| `AdminShell` | 单一导航栏、内容 `min-width: 0`、窄屏折叠；不在每页保留标注员目录占宽 |
| `AnnotatorShell` | 稳定顶部信息/用户入口；编辑区获得主要宽度 |
| `PageHeader` | 标题、辅助说明、主操作；支持长标题而不推出按钮 |
| `FilterBar` | CSS Grid 控制列宽和换行；长标签、多选、错误、重置入口 |
| `DataTable` / `CursorPager` | 薄业务组合；服务器 cursor 用“加载更多”或已访问页历史，不伪造任意页码 |
| `AsyncState` / `StatusBadge` | Loading、Empty、Error、Read-only；标签与图标共同表达状态 |
| `SaveStatus` | 本地持久化、正在同步、已同步、离线、冲突、存储失败清晰区分 |
| `AudioTransport` / `WaveformPanel` | 生命周期独立；播放刷新不能带动全部文本重渲染 |
| `TranscriptRow` | 局部 RTL、稳定分段 key、标签/校验、焦点与光标保持 |

通用 Button/Input/Select/Modal 直接使用库组件；不包装每个 props，不创建通用“万能页面 JSON schema”。复杂业务详情和破坏性确认组合到相应迁移阶段再做。

### 6.3 代表页结构

管理页顺序：导航 → 页面标题 → 日期及常用筛选 → 可折叠高级筛选 → 匹配数/时长 → 表格 → cursor 操作。将 U01 的长筛选组合放入样例矩阵，避免只测三个短输入框。

工作台顺序：应用栏 → 任务标题/保存状态 → 播放栏 → 紧凑波形 → 分段编辑 → 折叠的场景核验。P1 默认波形高度约 120px，展开可以放大；不继承旧版把正文推到首屏以下的顶部高度。去掉右侧常驻排行榜。

390px 下分段每行改为“序号/时间/操作在上、正文占整行”的布局；UI 仍 LTR，正文明确 RTL，编号/时间/文件名独立方向隔离。不要缩小到无法输入的字号来避免溢出。

## 7. 管理代表页：Corpus 只读纵向切片

### 7.1 API 与字段

| 请求 | P1 用途 |
| --- | --- |
| `GET /api/admin/session` | 检查独立管理员身份；保留现有 session/CSRF 生命周期 |
| `GET /api/admin/metadata/facets` | 获取筛选选项；选项未加载时正确 disabled/error |
| `GET /api/admin/tasks` | 列表、cursor、匹配条数、匹配时长 |

`admin_tasks` 已返回 `items`、`next_cursor`、`matched_count`、`matched_duration_seconds`、`applied_filters`、`filter_digest`。匹配数量/时长直接使用此响应，不额外请求 overview，不用已加载行数冒充总量。

行类型保留任务 ID、filename、status、duration、当前提交者/assignment、source_scenes、source_confidence、review_status 等实际字段。`task_status`、`status`、`pool_state` 不是同义词；不要为适配样式改后端含义。P1 不开放批量选择或写操作。

### 7.2 交互要求

1. 支持搜索 q、status、日期预设/自定义，以及来源场景、来源可信度、批次、核验状态筛选。高级项可折叠，不能靠整页横滚查看。
2. UI URL 保留 `view=corpus`、`range`、`from`、`to`、`q`、`status` 语义；新增代表页筛选可使用后端同名 query，并在旧入口不支持的参数处说明边界。
3. UI 自定义结束日是包含当天；API `to` 按旧 `commonQuery` 加一天形成排他边界，传 `timezone=Asia/Shanghai`。不要将 UI 的 to 原样传 API，也不要用浏览器 UTC 转换改变日期。默认 range 沿用旧 corpus；不要把 cross-check 全时段规则搬过来。
4. 每批默认 limit=50，加载更多使用原 `next_cursor`。所有筛选改变后清空旧列表/cursor；query key 含角色、session 范围和全部标准化筛选，cursor 不跨 filter_digest。
5. 新筛选发出后旧响应不能覆盖新列表。取消请求与响应归属校验配合；重试按钮用当前条件，不复活旧 query。
6. 只读 GET 明确 retry/refetch 策略：401/403/400 不重试，临时错误最多有限次；窗口重新聚焦不自动重排用户正在查看的列表。编辑器不复用这些 GET 重试规则。[TanStack Query 默认行为](https://tanstack.com/query/latest/docs/framework/react/guides/important-defaults)。
7. 加载、空、首屏失败、加载更多失败分别展示；追加失败保留已加载行及可重试入口。切换管理员身份清除该角色的 Query 缓存。
8. 长文件名限宽但有完整查看/复制途径；数字右对齐。小屏表格可在独立容器横滚，筛选和页级操作不能一起被推出屏幕。
9. 登录先使用旧管理入口，登录后按说明进入预览 URL；P1 不新增假的管理员认证页。session 失效则清空敏感查询并回旧登录入口。

## 8. 工作台代表页与最小保存验证

### 8.1 可操作范围

- 使用 `GET /api/current-user`、`GET /api/assignment`、`GET /api/scenes` 取得当前会话、已领取任务及 taxonomy/flags；无 assignment 时给出空状态和返回旧工作台入口，不能隐式 claim。
- 验收 fixture 经真实登录/claim API 预先准备 assignment，再打开预览。只读模式、离线、401/403、409 等由测试场景构造。
- 编辑正文、起止时间和排除训练标记；场景核验保留可选性、flags，并验证只改核验也能保存。P1 可以只覆盖证明协议所需的最小核验输入组合，不宣称完整场景 UI 已迁移。
- 支持手动保存、自动保存、Ctrl/Cmd+S；不提供无实现的 Complete/Skip/Abandon。返回旧工作台使用完整导航，先确保本地稿持久化。
- 使用现有 `/api/audio/<task_id>` 与 `/api/waveform/<task_id>`：播放/暂停、进度、选中段播放、段尾停止、选段与高亮联动。完整拖拽边界编辑留 P4，但在 P1 用时间输入验证边界值和保存。

### 8.2 分离三类生命周期

1. **Editor store**：分段值、dirty、选中行、核验值；稳定 segment ID 为 key。编辑一行不重建全表；视图卸载不丢内存稿。输入法 composition 期间不重写文本/跳焦点，不做 trim/NFC 等隐式归一化。
2. **Media controller**：同一任务持有同一个 audio 实例，timeupdate/动画帧只更新播放游标与时间显示；换任务/最终 dispose 才停止并清监听。加载迟到的波形不能覆盖下一任务。
3. **Persistence/session controller**：在页面组件外管理队列、IndexedDB、活动心跳、冻结。心跳周期和期限读取现有 session 配置；沿用可信活动事件语义，presence 不能无限延长 idle/absolute 期限。React StrictMode 的 subscribe/unsubscribe 重放不创建第二个队列或心跳；最终退出应能 dispose 资源。

建议 controller 至少有 `attachAssignment(context)`、`subscribe`、`getSnapshot`、`updateSegment`、`updateReview`、`persistLocal`、`flush`、`freeze`、`dispose`。函数名称可调整，职责和“先本地持久化再发不可变请求”的顺序必须清楚。不要用一个 effect 同时承担 fetch、媒体、编辑和保存。

### 8.3 保存协议与旧存储

沿用 [P0 存储契约](frontend-rebuild-p0/storage-contract.md)：`annotation-offline-v1` / version 1 / `working_drafts` / `outbox`，不新增不兼容 schema，不清空其他用户记录。

优先用带 TypeScript 类型的 adapter 调用现有 `static/offline-drafts.js`，同源外链加载；P1 不复制一份不同语义的 IndexedDB 实现。若必须抽取模块，保持旧全局接口兼容并跑双向回归。`annotator-session.js` 包含 DOM/样式逻辑，不能未经 CSP 与生命周期检查就直接作为 React 会话服务加载；可以抽出纯协议服务供两端使用，避免同时跑旧新两套心跳。

保存必须满足：

1. PATCH `/api/assignment/current` 保留 `lease_token`、`expected_revision`、`operation_id` 和实际 segments payload；`scene_review` 按当前契约包含。请求送出前写入 outbox，冻结 method/route/body。
2. 同一 assignment 最多一个在途写请求。输入 A 发出后继续输入 B，A 成功只清除仍等于 A 的 dirty；B 继续排队，新 operation 使用服务器确认后的 revision。
3. 服务端已接收但响应丢失时，重放原 ID/原 body，不能重新生成 operation；测试必须真的让请求到达服务器后丢弃响应，不能用“发送前断网”冒充此场景。
4. 单独修改 scene_review 也排队。`scene_review_write` 关闭时仍可保存正文，不发送非法核验写入。
5. 离线/暂时性错误有限退避，不忙循环。401 或停用 403 立即停止后续写和重试；先尝试保存本地稿，再导航。revision 409 保留本地稿，明确冻结/导出/丢弃流程，不自动刷新覆盖输入。
6. IndexedDB 不可用、quota、损坏记录是独立状态；本地保存失败不显示已保存，不能清空未确认 outbox 后假装恢复。
7. actor/session generation、task、version、lease 变化后旧请求不能更新新上下文。Query refetch 不能重置 dirty 编辑内容。
8. 用户可见 UI、错误、导出不得泄漏复标 `mode` / `round_id`。保留原字号偏好 key。
9. P1 controller 只执行本阶段支持的普通保存。进入 assignment 时先检查本用户/任务的存量 draft/outbox，再启用编辑和自动保存。读取到旧版 complete/abandon outbox 时，必须保留并引导回旧工作台接续，不重放成 PATCH、不丢弃、不覆盖对应旧稿、不允许越过终结请求继续保存。
10. 新日志函数处理异步 fetch rejection，上报失败不递归上报；截图、日志、验收产物不输出 cookie、CSRF、密钥或用户转写正文。

### 8.4 必须演示的保存场景

| 场景 | 判断结果 |
| --- | --- |
| 普通修改并刷新 | 真实 API 已保存，刷新后值与 revision 一致 |
| A 在途，继续输入 B | A 确认不清 B，后续同步 B，无并行 PATCH |
| 服务端成功、响应丢失 | 原 ID/原 body 重放只生效一次，新输入继续保存 |
| 断网编辑→刷新已有页面→重连 | 相同用户/任务的本地稿恢复，确认后 outbox 清除 |
| 核验单独修改及 flags 关闭 | 前者可保存；后者正文保存不受阻 |
| 401 / account_deactivated 403 / revision 409 | 冻结规则正确，稿件可保留/导出，不假报成功 |
| StrictMode 与重复挂载 | 一个心跳/一个队列；不重复发出相同写操作 |
| 旧页→新页→旧页 | 至少普通 PATCH 的 dirty draft/outbox 双向可接续；从实际旧页生成记录，不只注入 P0 的临时 UUID 快照 |
| 旧终结 outbox | 新页识别为本阶段不支持并保留，回旧页仍可接续 |
| 存储失败 | 显示真实失败，不清稿、不显示“已在本地保存” |

完整终结请求互通、多用户多任务组合、历史/纠正的迁移仍由 P4 承担。上述 P1 最小切片先证明技术路线，不以“只是样例”为由省略普通保存的数据正确性。

## 9. 测试、视觉与性能验收

### 9.1 新增有业务价值的测试

下表路径是**待新增文件**，实现时可合并，验收覆盖不得减少。

| 文件建议 | 必需断言 |
| --- | --- |
| `tests/test_frontend_delivery.py` | 开关默认关闭、角色鉴权、reason、精确路由、产物缺失、manifest/chunk 完整性、MIME/缓存、穿越/未知资源404、API不回HTML |
| `tests/browser/test_frontend_p1_components.py` | Flask 生产构建下两主题主要控件/弹层可操作；CSP事件、console/pageerror、焦点恢复、无外部资源 |
| `tests/browser/test_frontend_p1_corpus.py` | 实际返回数据、matched_count/时长、日期排他上界、cursor、筛选重置、迟到响应、追加失败保留行 |
| `tests/browser/test_frontend_p1_workspace.py` | 长阿拉伯语/混排、首屏、窄屏、字号、光标、Ctrl/Cmd+S 作用域、音频实例及分段停止 |
| `tests/browser/test_frontend_p1_persistence.py` | 第 8.4 节所有最小保存场景，验证真实请求及 IndexedDB 结果 |
| `frontend/src/test/*` | 仅纯逻辑：query 编解码/日期边界、队列确认不清新输入、冻结/退避状态迁移 |

固定夹具至少包括：长英文/阿拉伯语文件名、长用户名、多行阿拉伯语+数字+标点+emoji、空任务列表、首屏失败、重复筛选与迟到响应、100/500/1000 分段。IME 自动测试可模拟 composition 状态，但不能据此声称已验证真实系统输入法；人工结果或未测范围单列。

现有测试保持默认旧入口；将其业务断言用于新预览专项测试，不全局替换旧 DOM 定位器。重点参考：

- `tests/test_api.py::test_pages_require_login_and_completed_route` 与 `test_audio_range_and_object_authorization`。
- `tests/browser/test_regression_corpus_date_stats.py`。
- `tests/browser/test_regression_review_save_queue.py::test_lost_save_response_retries_identical_body_then_saves_new_review`。
- `tests/browser/test_regression_review_only_save.py::test_review_only_manual_save_survives_reload`。
- `tests/browser/test_session_takeover.py` 中离线、丢响应、新输入保留、revision conflict 与未授权前持久化场景。
- `tests/browser/test_cross_check_workspace.py` 中盲标及导出不泄漏模式场景。

### 9.2 视觉完成标准

P1 自动验收浏览器为固定版本 Chromium，沿用现有 CI。Firefox/WebKit 属于未验证范围，须在报告中明确；Chromium 通过不代表已承诺跨浏览器支持。

两代表页在 1920×1080、1440×900、1366×768、1024×768、390×844 均截图并检查；弹层至少覆盖桌面、小屏、两主题。

- 文档 `scrollWidth <= clientWidth + 1px`。若表格横滚，只发生在表格容器；不得用 `body { overflow-x: hidden }` 掩盖被裁切操作。
- 1366×768 下首个正文框位于视口内，至少一个可编辑行完整可见；保存状态和播放入口同时可见。
- 390×844 下正文框 left ≥0、right ≤视口宽，首个正文可直接聚焦；主要按钮、错误、冲突导出入口可达。
- 同排工具栏控件高度差 ≤1px；标签换行/错误提示不破坏输入基线。长文件名不推出操作按钮。
- 200% 缩放下完成关键操作；不能用 Playwright deviceScaleFactor 代替浏览器缩放证据。若环境不能自动设置，记录人工检查方式和结果。
- 弹层在打开、页面滚动和 resize 后仍可见、未被裁切；Tab/Escape/关闭后焦点恢复正确，持续错误不靠短暂 toast。
- 图标有 accessible name，错误/状态不只靠颜色。正文保持原始 Unicode 内容、局部 RTL 和选择区。

截图固定 Chromium、en-US、Asia/Shanghai、字体、deviceScaleFactor、reduced motion 与种子数据。动态 UUID/时钟可定点屏蔽，布局和关键状态不能整块遮盖。保存新截图和 P0 对照，第一次 P1 基线要人工逐页检查，不批量接受未知差异。

### 9.3 性能门槛与测量限制

复用 P0 的 100/500/1000 合成档位、相同数据与 input→第二次 requestAnimationFrame 定义。每档至少独立运行 3 次，每次至少 24 个输入，保留原始样本，记录浏览器/CPU/网络条件；不与回归测试并发测量。

| 项目 | P1 判定 |
| --- | --- |
| 输入正确性 | 全档位无丢字、光标跳动、composition 被截断；为阻断项 |
| 100 分段交互 | 各次 p95 与中位汇总均记录；中位 p95 目标 ≤50ms，不达标需优化后复测 |
| 500/1000 压力档 | 继续以 ≤50ms 为设计目标；超标必须给出 trace、瓶颈和后续决策，不冒充已满足总计划性能目标 |
| 页面重渲染 | 编辑一行不更新整张表/图表；播放游标不驱动全页 commit，用 React Profiler/trace 举证 |
| 媒体和资源生命周期 | 20 次代表任务切换/remount 后无重复 audio/interval/listener 持续累积；P5 再做完整50任务场景 |
| 资源体积 | 记录两入口首访 JS/CSS 的 raw/gzip、chunk、请求数及长任务；路由之间不提前下载另一个完整业务模块 |

P0 三档输入 p95 为 49.2 / 58.3 / 91.2ms，来自一次采集；不得把旧单次值与新最快一轮直接比较后宣布提升。如果宣称性能改进，应在同环境重新采旧版对照，写入新证据目录。

虚拟化不是 P1 默认前提。先做行订阅与媒体隔离；仍有显著性能问题时，以数据决定是否引入专用虚拟列表，并补焦点、变高行、滚出视口不丢稿验收。若采用虚拟化，P0 的“所有 textarea 挂载完成”耗时不再可直接比较，应另外报告首屏可编辑耗时，并验证全部分段仍可导航/保存。

P0 没有资源体积基线，本文不编造压缩包预算。P1 报告必须根据实测为 P2 起固定入口 gzip 上限及测量方法，可采用本次稳定构建实测值上浮 10% 作为变更告警线；这是回归线，不能替代交互性能验收。真实分段分布未取得前，压力档性能例外可记录为带条件通过，但输入/保存/CSP/布局不能例外。

### 9.4 CI 和运行次序

1. 新增前端 job：安装固定 Node/npm → `npm ci` → typecheck → lint → Vitest → 两种 build → check:dist。构建 artifact 供后端 job 下载，不能只上传源码给后端测试。
2. 保留 `.github/workflows/test.yml` 的 PostgreSQL 16/18 两个 job 与现有测试 runner；在测试启动前放好 `dist/`、`dist-p1/`。
3. 新浏览器测试 fixture 仅针对自身开启预览，测试结束恢复配置；旧测试保持默认关闭。dist 缺失在 CI 是失败，不能 `pytest.skip`。
4. 先运行变化对应的定向检查；完成后跑一次完整 pytest 包括新增 P1 测试。PG16/18 结果分别记录；本地只有16时明确18由CI验证，不能用本地16代称矩阵通过。
5. 另做默认关闭、standard/p1 构建与预览开关组合检查；保存命令、退出码、数量、跳过原因和截图。

实现后的本地命令约定（当前尚未存在的命令不得写作“已运行”）：

```bash
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run build:p1
npm --prefix frontend run check:dist
uv run --no-sync pytest -q tests/test_frontend_delivery.py tests/browser/test_frontend_p1_*.py
uv run --no-sync pytest -q
```

独立机器先 `uv sync --frozen --group dev` 和 `uv run playwright install --with-deps chromium`。测试使用仓库一次性数据库 fixture；CI 的 `scripts/run_pytest_with_postgres.py` 继续保留匹配版本客户端的检查。不要使用会主动覆盖 P0 目录的默认采集命令来存 P1 结果。

## 10. 实施完成时的交付清单

在 `docs/plans/frontend-rebuild-p1/` 至少提供：

| 文件 | 内容 |
| --- | --- |
| `README.md` | 构建提交、基线、实际完成范围、运行入口、结论与限制 |
| `component-decision.md` | 组件库/版本、CSP方案、实测及必要的取舍 |
| `acceptance.md` | D1–D8/本节检查表逐项结果，链接实际测试与证据 |
| `screenshots/` + 索引 | 两代表页、控件主题/弹层/关键错误；视口与状态清晰 |
| `layout-measurements.json` | 文档宽、编辑框几何、操作可达性与对齐数据 |
| `performance.md` + 原始 JSON/必要 trace | 三档输入样本、资源体积、媒体实例验证和未达标解释 |
| `test-results.*`、`build-manifest.json` | 实际命令/版本/退出码/计数、构建与资产校验信息 |
| `p2-handoff.md` | 可复用组件/API、预览配置、未迁移功能和下一阶段阻碍 |

报告使用“通过 / 失败 / 未执行 / 带条件通过”四种状态。P0 未取得真实数据、未测非 Chromium、未做真实 Nginx 上线检查继续作为明确边界；不要把未执行改写成通过。

完成检查：

- [ ] A–H 均已实现并有对应结果；正常构建与 P1 构建可重复生成。
- [ ] 所有旧正式入口/API 语义保留，预览默认关闭，关闭后无需前端构建也可跑旧系统。
- [ ] 唯一组件体系已确定；真实 CSP 下两主题和主要交互通过。
- [ ] 管理代表页使用实际 API、匹配总量和 cursor，没有假页码或生产数据依赖。
- [ ] 工作台首屏/窄屏/阿拉伯语/音频验证通过。
- [ ] 第 8.4 节保存验证通过，普通 PATCH 旧↔新兼容，不删除未支持终结请求。
- [ ] 纯逻辑、浏览器、全量回归和 CI PG16/18 结果齐全；失败/跳过没有被弱化断言掩盖。
- [ ] P0 原始证据没有被覆盖；P1 新截图与几何/性能数据可复算。
- [ ] 生产部署和正式业务切换未纳入 P1 完成声明；P2/P4 剩余项清楚。

提交按工作包组织，最终报告说明改了什么、如何运行、实际验证和剩余限制。Git 推送/PR按交付任务的授权执行；本方案本身不要求生产发布。
