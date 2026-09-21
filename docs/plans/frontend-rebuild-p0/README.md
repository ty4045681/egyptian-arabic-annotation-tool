# 前端重建 P0 交付件

日期：2026-09-21。分支：`codex/frontend-rebuild`。业务源码基线：`ef7696d508608725069bcfca099e498fc8fc6885`。

**本地 P0 交付已完成：现状截图、问题清单、功能/API/测试矩阵、存储契约和合成规模基线均可复现。真实语料分段分布仍待只读业务库连接，已明确记录缺口并提供统计脚本。**

本阶段没有重写页面、安装前端框架、修改业务 API 或部署服务。基线使用一次性 PostgreSQL、合成音频和合成账号；旧验收图片没有被覆盖。

## 从这里查看

| 交付件 | 内容 |
| --- | --- |
| [截图画廊](gallery.html) / [截图索引](screenshots.md) | 46 张当前 UI 截图，含五种视口与关键状态；点击查看原图 |
| [问题清单](issues.md) | 10 项实测缺陷/设计改进/性能观察，附复现条件、证据和验收要求 |
| [功能—API—测试矩阵](feature-api-test-matrix.md) | 50 项能力、迁移位置、业务约束；另列 7 类覆盖缺口 |
| [浏览器存储契约](storage-contract.md) | IndexedDB 两个 store、全部工作稿/队列字段、偏好/flash/CSRF、旧新版本兼容要求 |
| [分段规模与性能](performance.md) | 100/500/1000 分段实测、方法限制和真实数据待补项 |
| [全量回归结果](regression-results.json) | 503 passed、0 failed、1 skipped；逐测试耗时与跳过原因 |
| [采集运行结果](capture-results.json) | 7 个采集场景全部通过；与现有业务测试分开记录 |
| [路由清单](route-inventory.json) | 从 Flask 实际 url_map 提取方法、入口、源码位置 |
| [源码指纹](source-manifest.json) / [证据校验](evidence-verification.json) | 环境版本、业务源码 SHA-256、原图 SHA-256、文件完整性 |
| [布局测量](layout-measurements.json) | 文档宽度、首个转写框坐标/大小、DOM 中转写框数量 |
| [离线快照](storage-offline-sample.json) / [重连快照](storage-recovered-sample.json) | 从真实浏览器 IndexedDB 导出的合成记录；含可对照的 key 和 schema |

## 最重要的发现

1. **复核队列整页溢出。** 1366px/1440px 视口下 document 宽度均为 2114px，部分筛选和操作跑到屏幕外。
2. **标注正文首屏不可见。** 1366×768 的长文件名样本里，首个 textarea 顶部约928px；390px 下正文左边界约506px，需先横滚。
3. **离线报错上报有未处理 rejection。** 原稿能在重连后保存，但 `reportError` 的 fetch 失败又触发未处理异常。
4. **1000 分段全量挂载约1.7万 DOM 元素。** 输入到第二帧 p95 约91ms；需要用同一档位约束新编辑器，不能将此当作真实用户分布。

这几项直接进入 P1 的代表页面验收：筛选栏不撑破页面、正文进入笔记本首屏、窄屏正文可直接编辑、保存与媒体状态独立于视觉布局。

## 实际执行结果

- 原有完整测试集：**503 passed / 1 skipped**，pytest 约707秒；其中浏览器测试 **77 passed**。
- 跳过项：`tests/test_annotation_speed.py::test_speed_query_plan_on_100k_current_versions`，需显式 `ANNOTATION_SPEED_EXPLAIN=1`。没有跳过普通浏览器验收来掩盖失败。
- 采集场景：**7 passed**，pytest 约86秒。布局问题记录为证据，不把“旧页面没有缺陷”设为采集成功条件。
- PostgreSQL：pgserver 自建的 **16.2**。本次结果不代表 PostgreSQL 18、线上 Nginx 或其他浏览器已验收。
- 浏览器：Chromium **151.0.7922.34**，Playwright **1.62.0**，en-US、Asia/Shanghai、device scale 1、reduced motion。
- 视口：1440×900、1366×768、1920×1080、1024×768、390×844。并非所有页面都跑五种尺寸；具体覆盖逐项列在截图索引。
- 覆盖状态：登录统计为空/已加载/加载中/失败、任务领取/编辑/场景验证/完成/纠正、历史与详情、离线/冲突/会话接管、六个管理入口、权限表单、撤销/停用预览、抽样设置、复核对照、日期筛选为空。
- 正常管理页 pageerror 为空；登录前 admin session 的401是正常流程。离线场景的网络失败为主动注入，额外的 reportError 未处理异常单独登记为 U09。

### 采集脚本修正记录

第一次采集把“任务池已耗尽”错误地当作仍应出现 Claim next task；第二次遗漏了 Playwright 对原生 correction confirm 的接受处理。均为采集脚本问题，业务页面未修改。修正后完整采集七项通过；前两次结果保留在 `attempts/`，不会被算作业务基线失败。

第一次规模采集与回归并发，其原始结果保留在 `attempts/initial-scale-*.json`。正式性能表采用全量回归结束后的最终独立采集。最初回归 runner 的退出日志曾遇到已关闭重定向流；不影响503项测试结果和数据库停止，后续 runner 已在重定向前初始化应用日志。

## 可重复运行

在仓库根目录，使用已有 uv 开发环境与 Chromium：

```bash
UV_CACHE_DIR=/tmp/annotation-review-uv-cache \
UV_PYTHON_INSTALL_DIR=/opt/annotation-python \
uv run --no-sync python scripts/frontend_p0.py regression

UV_CACHE_DIR=/tmp/annotation-review-uv-cache \
UV_PYTHON_INSTALL_DIR=/opt/annotation-python \
uv run --no-sync python scripts/frontend_p0.py capture

uv run --no-sync python scripts/frontend_p0_report.py
```

环境变量路径是本机已有安装路径，其他机器可用自己的 uv 环境；需要 `uv sync --frozen --group dev` 与 `uv run playwright install chromium` 安装相同依赖。默认输出本目录，可用 `--output` 指向新的证据目录，避免覆盖已经用于评审的一次采集。

runner 主动移除继承的业务/外部测试 DSN 与 PG bindir，由现有 fixture 创建一次性集群和唯一测试库。截图 helper 被定向到本目录 `regression-screenshots/`；旧的已提交验收截图保持原样。日志 `.log` 是本地诊断文件，结构化 JSON 和 JUnit XML 才是纳入交付的结果。

脚本复现的是相同场景，不承诺每次图片字节相同：临时 UUID、日期、计时器和动态反馈会变化。正式视觉回归需在 P1 固定/屏蔽这些动态区域；本次已记录每张实际原图的指纹。

## P0 完成检查与未完成项

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 当前页面可运行 | 完成 | 全量测试和7个采集场景 |
| 页面/状态截图基线 | 完成 | 46张原图、索引、几何测量 |
| 问题优先级与验收 | 完成 | U01–U10 |
| 功能/API/测试/迁移位置 | 完成 | F01–F50；覆盖不足项明确列为 G01–G07 |
| 浏览器持久化结构 | 完成 | 源码契约 + 实际离线/重连 IndexedDB 快照 |
| 合成分段规模基线 | 完成 | 100/500/1000档位与原始时间样本 |
| 真实任务典型/最大分段 | **待只读数据源** | `real-corpus-profile.json` 明确 unavailable；只读统计脚本已交付 |
| 业务源码保持基线 | 完成 | `evidence-verification.json` 逐文件 SHA 与基线提交比对 |

可以依据这些交付件开展 P1 的布局与组件验证；真实分布补齐前，虚拟化阈值与真实最大任务的性能承诺暂不定案。
