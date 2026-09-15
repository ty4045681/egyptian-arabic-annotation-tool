# 全英文与十场景升级验收

2026-09-15，业务提交 `73b8b9c73f3f3ea4fd8b60ecb03b20898eeebce4`，由腾讯云上的 Grok 4.6 xhigh 完成。平台文案改为英文，十个场景按用户指定名称和顺序显示。来源缺失、NULL 场景和明确的 Spoken languages 使用同一个来源分类；置信度、模型结果和人工核验各自保持含义。

PostgreSQL 16.2 全套254项通过（80.60秒），PostgreSQL 18.6 全套254项通过（85.39秒），均包含15项真实 Playwright 浏览器测试，无跳过。独立验收增加了英文界面/十场景顺序、未知来源去重、组合筛选同一证据、权限撤销与旧字段兼容、历史模型标签、重新打开任务来源显示等检查。截图审查发现的字号控件阿拉伯语标签已改为英文 Arabic；实际转写和来源原文不翻译。

迁移005只更新场景目录；原有001–004保持原样。迁移及重复执行、NULL来源和历史预测保留、导出恢复均有回归覆盖。升级/兼容回退说明见[DEPLOY.md](../../../DEPLOY.md)。

## 独立容量检查

在同一腾讯云4核/约8GB主机，PostgreSQL18.6、Gunicorn 2 workers × 4 threads 下独立运行。数据为100,000任务、300,589当前来源、308,189全部来源版本和400,000分段。混合场景样本加入 Spoken languages。认证在计时前完成；管理员列表/概览各40次，领取使用20个并发客户端。查询诊断在HTTP测量结束后运行。

| 场景 | 样本 | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|
| 普通新领取（并发20） | 80 | 99.27 | 184.47 | 209.68 |
| 管理员50条列表 | 40 | 29.37 | 60.60 | 67.81 |
| 10万任务概览 | 40 | 1438.01 | 1918.45 | 2138.08 |
| 混合权限并发20 | 20 | 95.03 | 151.77 | 348.29 |
| 稀少Taxi场景并发20 | 20 | 3739.09 | 5335.73 | 5491.73 |

普通领取/列表/概览三项既定p95门槛500/500/2000ms均通过。独立重算原始样本分位数，并逐项核对220个成功新领取任务ID全局不重复、置信度优先级与数据库一致、混合权限均符合范围；所有列表和概览响应数量正确。

稀少Taxi场景p95为5.34秒，空场景一次409响应为3.30秒，仍需后续优化。这两个场景没有包含在上述三项p95门槛中，本次没有宣称所有响应都低于2秒；本次混合场景数据分布与旧报告不同，不能仅凭这些数值认定代码回归或改善。

HTTP负载期间约100ms间隔采样870次，未观测到Lock等待，死锁为0。短于采样间隔的等待可能漏采。实际仓储入口记录33条SQL，其中26条SELECT/CTAS-SELECT执行计划；最大单个排序34,222KB、哈希24,601KB，最多2个并行worker，临时写入块最大1,147。上述值不是整个服务的并发内存上限。

## 证据与复现

本目录保留原始HTTP样本、数据规模、查询计划、等待采样及独立校验结论。临时数据库连接串从仓库副本省略；校验后仅删除本次生成的合成集群。真实数据库、音频与原基线预览没有参与本次检查。

在没有其他测试或压测任务运行时，从仓库根目录执行：

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/load_test_100k.py --pg-bindir /usr/lib/postgresql/18/bin \
  --keep --artifact-dir /tmp/annotation-english-capacity
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/profile_capacity_queries.py \
  --results /tmp/annotation-english-capacity/results.json \
  --output /tmp/annotation-english-capacity/actual-query-plans.json
```

`--keep`用于后续诊断；普通压测省略它会自动清理合成集群。
