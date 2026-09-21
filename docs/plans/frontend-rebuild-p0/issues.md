# P0 界面问题清单

基线源码：`ef7696d`。截图来自当前代码、隔离 PostgreSQL 和合成数据。以下分别标注实测缺陷与设计改进，避免把个人偏好当成程序错误。精确几何数据见 `layout-measurements.json` 与各 `capture-*.json`。

## 优先处理

### U01 · 复核队列让整个页面横向溢出（高，实测缺陷）

- 复现：管理员登录 → Cross-checks → Awaiting review；视口 1366×768 或 1440×900。
- 实测：两种视口下 `document.documentElement.scrollWidth` 均为 **2114px**，分别超出视口 748px、674px。多个筛选项和表格右端操作落在视口外。表格自己已有滚动容器，仍没能限制整页宽度。
- 证据：[1366px 队列](screenshots/admin-cross-check-queue-1366x768.png)、[1440px 队列](screenshots/admin-cross-check-queue-1440x900.png)、`capture-admin.json`。
- 来源：`admin.html` 的 Cross-check 筛选栏、`admin.css` 工具栏布局及 `static/admin-cross-check.js` 的渲染。
- 迁移方案：统一 `FilterBar`，标签上置、字段网格换行，高级筛选按需展开；表格宽度限制在明确滚动容器内。
- 验收：1366/1440/1920px 整页宽度不超过视口（允许 1px 取整误差）；所有筛选项可达；只有表格容器允许横滚。

### U02 · 常见笔记本尺寸首屏看不到转写输入（高，实测可用性问题）

- 复现：12 个分段、长文件名、长用户名；填写第一条转写后回到页面顶部。
- 实测：1366×768 下第一个 textarea 顶部约 **928px**，1440×900 下同样约 **928px**；1024×768 下约 **964px**。首屏主要被任务工具栏、可选场景核验和波形占满。
- 证据：[1366px 首屏](screenshots/workspace-long-arabic-1366x768.png)、[完整页面](screenshots/workspace-long-arabic-full-1366x768.png)、`capture-annotator.json`。
- 来源：`index.html` 的 taskbar/scene-review/wave-panel/table-panel 顺序和尺寸。
- 迁移方案：压缩稳定任务栏；场景核验用紧凑摘要展开；波形初始紧凑且可折叠；编辑正文成为主要内容。
- 验收：1366×768 默认首屏能看到任务/保存状态、播放操作和至少一条可编辑转写；展开辅助面板是明确动作。

### U03 · 窄屏进入分段表后正文仍在屏幕右侧（高，实测可用性问题）

- 复现：同 U02，视口 390×844。
- 实测：第一条 textarea 的左边界约 **506px**，宽 220px；即使纵向滚到该行，正文初始仍在 390px 视口外。播放、序号和时间列占据可见宽度。
- 证据：[390px 工作台](screenshots/workspace-long-arabic-390x844.png)、`capture-annotator.json`。
- 判断：页面本身没有横向溢出，这一点是正确的；问题是转写这一主要操作不在初始可见区域。
- 迁移方案：窄屏分段使用正文优先布局，播放/时间进入一行紧凑工具栏或可展开区域。若继续用表格，也应明确支持正文优先的列策略。
- 验收：390px 下无需先横滚即可编辑当前分段；时间与质量操作仍可达；RTL 输入和选择文本正常。

## 布局与一致性改进

### U04 · 长文件名把任务栏撑高，操作与媒体分散（中，实测设计问题）

- 当前长文件名折成多行，播放器与跳过组占右侧，而保存/完成/放弃另起一行；手机又形成多层按钮行。
- 证据：[桌面任务栏](screenshots/workspace-long-arabic-1366x768.png)、[手机任务栏](screenshots/workspace-long-arabic-390x844.png)。完整文件名可被换行显示，不能把它误报为数据丢失。
- 方案：任务标题区域使用有上限的显示和完整查看/复制入口；媒体与操作各有固定位置；保存反馈预留宽度。
- 验收：短/长文件名、离线/保存失败切换时，完成按钮位置稳定；不把控制区无限向下推。

### U05 · 场景权限表单与管理端其他控件不一致（中，设计改进）

- 人员详情中 Scope mode 和 Reason 采用行内标签，场景 checkbox 紧密排列；与页面其他字段、按钮、卡片的排列方式不同。
- 证据：[场景权限表单](screenshots/admin-annotator-scope-1440x900.png)；`static/metadata.css:68–71` 与 `admin.html` 的 `sceneScopeForm`。
- 方案：使用统一 Form、Select、Checkbox.Group 与字段网格；表单状态/错误放在稳定位置。
- 验收：标签、输入、帮助和错误在统一网格中对齐；长场景名与窄屏换行不改变字段对应关系；加载中仍禁用写入。

### U06 · 工作台排行榜长期占用编辑宽度（中，设计改进）

- 当前桌面右侧常驻约 252px 的贡献区，即使为空也保留整列；长转写在 1366px 下只分得约 327px 宽度。
- 证据：[工作台空排行榜](screenshots/workspace-long-arabic-1366x768.png)、`.app-shell` 两列声明与 `capture-annotator.json`。
- 方案：从编辑主区移到导航内的统计入口/抽屉，在空闲或登录页面查看贡献。
- 验收：转写正文获得主要宽度；统计功能仍可找到；不引入另一个常驻低频侧栏。

### U07 · 标题重复、统计卡片权重接近，队列位置偏后（中，设计改进）

- 管理端顶部和内容区重复标题；Cross-checks 用两行八张摘要卡片，真正队列在其后。移动 Overview 首屏主要是 KPI 和图表。
- 证据：[复核队列](screenshots/admin-cross-check-queue-1366x768.png)、[手机 Overview](screenshots/admin-overview-390x844.png)。移动 Overview 的内容密度是设计取舍，不是功能故障。
- 方案：一个 PageHeader；最关键指标保留视觉强调，其余以紧凑明细呈现；复核页把待处理队列置于主要位置。
- 验收：用户能快速定位待处理事项；存量、期间活动、训练资格等不同口径仍被明确表达，不能为减少卡片而合并口径。

### U08 · 四个入口维护不同的视觉控件规则（中，源码与截图确认）

- 登录、工作台、个人记录内嵌各自 CSS；管理端使用另一份独立 CSS，场景与复核又追加样式。
- 标注端金色标题、管理端黑色品牌块、字符图标、原生音频控件、胶囊按钮及不同弹窗实现并存。
- 证据：[登录](screenshots/login-empty-statistics-1440x900.png)、[工作台](screenshots/workspace-long-arabic-1366x768.png)、[管理登录](screenshots/admin-login-1440x900.png)、`source-manifest.json`。
- 方案：一个组件库、一套尺寸/字体/间距、两个主题映射、统一 SVG 图标。保留真实业务状态色，去掉发光标题与无功能装饰。
- 验收：公共控件和页面组合集中维护；新页面不增加同义 CSS 变量和独立弹窗实现。

## 可靠性与规模观察

### U09 · 断网报错上报自身产生未处理异常（中，实测缺陷）

- 复现：工作台编辑后切断浏览器网络，再点 Save draft。
- 结果：UI 正常进入 Save failed，重连后恢复保存；同时浏览器记录未处理的 `TypeError: Failed to fetch`。
- 已确认堆栈：`reportError → api → drainSaves → manualSave`。`index.html:332` 的 `reportError` 用同步 try/catch 包住 fetch，但没有处理 Promise rejection。
- 证据：`capture-persistence.json` 的 `page_errors`；[离线状态](screenshots/workspace-offline-save-error-1366x768.png)。有意注入的网络失败属于场景条件，未处理的报错上报异常是额外缺陷，不能混为“预期控制台日志”。
- 方案：在迁移请求/日志服务时显式处理上报失败；原始保存错误和本地稿件状态保持可见。
- 验收：离线保存失败可见、有限退避、重连成功，且无额外未处理 rejection；上报失败不递归上报。

### U10 · 分段表全量渲染，需基于规模决定优化（中，性能观察）

- 100/500/1000 分段分别全部出现在 DOM；没有虚拟化。大量分段会增加页面高度和 DOM 元素数。
- 证据：`scale-100.json`、`scale-500.json`、`scale-1000.json`；[1000 分段首屏](screenshots/workspace-1000-segments-1366x768.png)。最终数值与测量限制见 `performance.md`。
- 判断：这是容量风险与基线，不等同于已证明真实用户会遇到 1000 分段卡顿；目前没有真实库分布。
- 方案：P1 先隔离编辑行订阅和 Canvas 更新，结合真实 p95/max 决定是否虚拟化；若虚拟化须保留输入/焦点/草稿，不以卸载编辑行换取丢稿。
- 验收：相同夹具的输入延迟、首次渲染、DOM/监听器增长有可比较结果；不能仅比较 Lighthouse 分数。

## P0 已验证、应保留的行为

- 现有全量回归 **503 passed / 1 skipped**，其中浏览器测试 **77 passed**；跳过的是需显式启用的 10 万数据 EXPLAIN，不是普通浏览器测试。
- 本次捕获的正常管理页无未处理 JS 异常；登录前查询管理员 session 返回 401 是正常鉴权结果。
- 断网草稿确实进入 IndexedDB outbox；重连成功后 outbox 清空并更新 revision。
- 管理端已有严格 CSP、no-store、frame 防护；组件迁移不能靠放宽这些响应头解决兼容性。
- 手机工作台的页面边界没有溢出；问题 U03 限定为主要编辑列在容器内的可达性。

以上问题本阶段只记录，不修改业务页面。P1 应优先用 U01–U03 的数据和尺寸检验共享布局，而不是用短标题、少量英文示例判断页面是否完成。
