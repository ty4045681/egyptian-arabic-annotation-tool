# PostgreSQL 18 独立容量验收

2026-09-14，在腾讯云4核、约8GB主机完成。业务实现 `ef237b9` 由远端 Grok 4.6 xhigh 完成；独立验证工具提交 `d7ab426`。实际 PostgreSQL 18.6，Gunicorn 2 workers × 4 threads。完整回归240项（含13项真实浏览器测试）通过，耗时75.28秒；随后调整测试会话收尾后，5项相关验证工具回归通过，再单独运行本次容量验收。

数据：100,000任务、85,000待处理、300,589当前来源记录、308,189全部来源版本、400,000分段。新领取先完成认证，再以20个客户端同步发起第一批请求；每位用户只测一次新领取。管理员列表和概览分别顺序重复40次。HTTP测量包含服务端工作和响应传输，SQL诊断在HTTP测量结束后独立运行。

| 场景 | 样本 | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|
| 普通新领取（并发20） | 80 | 80.63 | 161.24 | 184.98 |
| 50条管理员列表 | 40 | 27.48 | 50.15 | 59.30 |
| 10万任务概览 | 40 | 1290.13 | 1799.51 | 2591.90 |
| 不同权限并发20 | 20 | 52.44 | 104.12 | 323.05 |
| 稀少出租车场景并发20 | 20 | 1188.26 | 1984.13 | 2078.20 |

三项既定p95门槛（500/500/2000ms）全部通过。普通新领取吞吐约210.2次/秒；所有成功场景合计220个新任务ID，全局无重复、无续领、无错误。独立逐项核对数据库中的来源置信度：普通/机场/购物为高，出租车来源为中；混合权限20人（11人全部、9人受限）均符合其场景范围。无权限用户返回409和`no_scene_access`。所有列表样本均为50条且匹配100,000任务，所有概览样本总量均为100,000。

`lock-waits.json` 在HTTP负载期间按约100ms间隔采样762次，未观测到Lock等待，采样器无错误；短于采样间隔的等待可能被漏过，不能据此宣称从未发生任何等待。数据库死锁计数为0。

稀少出租车场景p95仍约1.98秒；空场景的一次409响应约2.49秒，包含任务池统计。概览p99约2.59秒。本次通过的是明确的p95门槛，不是每次响应均低于2秒的承诺。这些边界保留为后续优化项。

## 实际SQL与资源设置

`actual-query-plans.json` 来自实际仓储入口：概览19条语句、列表6条、空池8条，共26条SELECT/CTAS-SELECT执行计划。它覆盖真实来源聚合、队列与元数据查询；基础`explain-list.json`、`explain-overview-matched.json`只是投影子查询，不能代表整个接口。诊断计时包含重复EXPLAIN的影响，不用于HTTP验收。

概览事务设置`work_mem=256MB`、`temp_buffers=128MB`、关闭JIT，最多2个并行查询worker，并使用局部planner cost设置。work_mem是单个排序/哈希操作的上限，temp_buffers按会话按需使用，不是整个服务统一预算。实际计划中最大单个排序约34,066KB、哈希约24,601KB；这些不是整条查询或并发请求的总峰值。空池来源统计有1,125个临时写入块。扩容进程/线程或增加其他负载后应重新测量。

## 复现

在仓库根目录、没有其他测试或压测任务运行时：

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py --bin-dir /usr/lib/postgresql/18/bin -q
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/load_test_100k.py --pg-bindir /usr/lib/postgresql/18/bin \
  --keep --artifact-dir /tmp/annotation-capacity-review
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/profile_capacity_queries.py \
  --results /tmp/annotation-capacity-review/results.json \
  --output /tmp/annotation-capacity-review/actual-query-plans.json
```

本目录保留本次原始响应样本、查询计划与独立校验结论。运行DSN已从仓库副本省略；临时集群在诊断与校验结束后已删除，复现时需先新建一次压测目标。`--keep`只用于诊断；普通压测省略它会自动清理。未使用真实音频或付费ASR调用，也未迁移正式数据库。
