# P0 分段规模与浏览器性能基线

## 本次实测

源码 `ef7696d`，Python 3.12.14、Playwright 1.62.0、Chromium 151.0.7922.34、PostgreSQL 16.2。localhost Flask、隔离 pgserver、合成静音音频；无 CPU/网络限速。视口 1440×900 进行测量，随后切换 1366×768 留存截图。

最终采集在全量回归结束后单独执行，避免主动并行测试干扰。样本数是人工构造的压力档位，**不是实际用户任务的典型值或最大值**。

| 合成分段数 | 领取至全部行挂载（ms） | 输入至第二帧 median（ms） | p95（ms） | max（ms） | DOM 元素 | 挂载转写框 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 738.68 | 29.25 | 49.20 | 60.70 | 1,896 | 100 |
| 500 | 1,555.00 | 36.00 | 58.30 | 59.00 | 8,695 | 500 |
| 1000 | 1,607.55 | 38.60 | 91.20 | 122.50 | 17,195 | 1000 |

原始样本：`scale-100.json`、`scale-500.json`、`scale-1000.json`；场景：`scripts/frontend_p0_capture.py::test_capture_segment_scale`。

## 测量定义与适用边界

- 领取耗时包括 Playwright 点击、localhost API、数据库和等待所有 textarea 挂载；不是单独的 React/DOM render duration，也不是生产网络首屏时间。
- 输入通过真实 `keyboard.insert_text` 发送；每档 24 次。input capture listener 开始计时，到第二个 `requestAnimationFrame` 回调结束。p95 使用排序后第 23 个样本，记录原始数组便于复算。
- 这个值是输入到可绘制帧的近似观察，**不是浏览器 Event Timing 的 INP，也不是操作系统实际像素呈现测量**。
- 先聚焦第一行再测输入；没有测量逐行 Tab、IME 组合、长文本粘贴、播放同时编辑、不同任务连续切换的内存稳定性。
- 样本是一次最终采集，不能据此声称统计显著的性能改进；后续比较应固定同一机器/数据/浏览器并增加重复次数。
- 未运行 10 万数据 EXPLAIN、生产 Nginx 部署负载测试或 PostgreSQL 18 矩阵。本次全量 pytest 的该 EXPLAIN 测试按项目默认明确 skip，原因见 `regression-results.json`。

## 对方案的影响

旧版全量挂载分段；1000 行的 DOM 与 p95 增长值得在 P1 用同一档位验证新编辑器。先隔离每行输入和媒体状态；如果需要虚拟化，要把数据状态保存在行组件外，验证滚出视口、切换任务、输入法、焦点与保存顺序。

不能因为总语料有 10 万条，就让管理列表加载 10 万行；总语料规模与单任务分段规模分开处理。管理端沿用服务端 cursor/pagination；是否虚拟化编辑表由真实单任务分布和实际体验决定。

## 真实语料分布：待只读数据源

当前进程没有 `ANNOTATION_DB_DSN`，本次未读取服务器凭据或连接 live 库。`real-corpus-profile.json` 明确记录 `status: unavailable`，没有用默认测试的两分段任务来冒充真实统计。

已交付可执行的只读聚合脚本：`scripts/profile_frontend_segments.py`。

```bash
# 由现有环境安全注入只读 ANNOTATION_DB_DSN，不把凭据写入命令或文档。
uv run --no-sync python scripts/profile_frontend_segments.py \
  --output docs/plans/frontend-rebuild-p0/real-corpus-profile.json
```

脚本只读事务、15s statement timeout、2s lock timeout；分别统计 draft 与当前 published 版本的分段 p50/p95/p99/max、超过100/500/1000的版本数、最大转写字符数。不输出用户、任务 ID、文件名、音频路径或转写内容；不迁移、不初始化业务连接池、不写数据库。没有 DSN 时退出码 2，避免把“未测量”当作成功。

真实统计补齐前，可以开展 P1 的组件与布局验证，但不能最终决定虚拟化阈值，也不能承诺覆盖真实最大任务的性能。
