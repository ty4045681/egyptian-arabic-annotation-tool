# 当前腾讯云生产数据库每日备份

此配置适用于 `/opt/annotation-production` 对应的 PostgreSQL 18 数据库
`annotation_production_20260915`，替代旧服务器 `/home/cjg`、`huawei` 路径的备份模板。
备份程序使用系统 Python 标准库和 PostgreSQL 工具，不依赖应用发布目录或虚拟环境。

- 每天 **北京时间 03:17** 执行，允许一分钟调度误差；停机漏跑时在恢复后补跑一次。
- 保留 **14 天**，目录为 `/data/annotation/production-db-backups`。
- 失败后每隔 15 分钟重试，两小时内最多启动三次；失败可在 systemd/journal 查看。
- 独立 Linux / PostgreSQL 用户 `annotation_backup`，通过本机 Unix socket peer 认证。
  数据库权限只有连接、schema 使用和表/序列读取；未来由 `annotation_prod_owner`
  在 public schema 创建的表/序列也自动授予读取权限。
- 每次执行完整 custom dump，恢复到独立、无 TCP 监听的临时 PostgreSQL 集群，
  验证恢复成功、必需表及迁移记录，记录所有用户表行数，并生成 SHA-256 清单。
  恢复成功后才发布备份目录、更新 `last-success.json` 和清理过期自动备份。
- 只清理此任务生成且验证成功的同库备份，不清理已有迁移/部署备份。
- 包含数据库全部表、序列和标注数据，不包含音频文件、数据库全局角色或服务器配置。
  当前备份位于本机数据盘，未配置异机副本。

## 安装与启用

在仓库根目录执行；若 `annotation_backup` 已存在，应先检查其现有用途和权限。

```bash
set -e
sudo useradd --system --user-group --home-dir /var/lib/annotation-backup \
  --no-create-home --shell /usr/sbin/nologin annotation_backup
sudo -u postgres psql -X -d annotation_production_20260915 \
  < deploy/tencent-production/backup-role.sql
sudo install -d -o annotation_backup -g annotation_backup -m 0700 \
  /data/annotation/production-db-backups
sudo install -o root -g root -m 0755 deploy/tencent-production/backup-postgres.py \
  /usr/local/libexec/annotation-backup-postgres.py
sudo install -o root -g root -m 0600 deploy/tencent-production/backup.env.example \
  /etc/annotation-production-backup.env
sudo install -o root -g root -m 0644 deploy/tencent-production/annotation-backup.service \
  deploy/tencent-production/annotation-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start annotation-backup.service
sudo systemctl enable --now annotation-backup.timer
```

首次 `start` 会等待真实备份及恢复验证完成；应在其成功后启用 timer。
更换生产数据库、主版本、迁移 owner 或 schema 时，需要同步调整配置和备份权限。

## 查看结果

```bash
sudo systemctl list-timers annotation-backup.timer
sudo systemctl status annotation-backup.service annotation-backup.timer
sudo journalctl -u annotation-backup.service -n 60 --no-pager
sudo cat /data/annotation/production-db-backups/last-success.json
# 将 <备份目录> 替换为 last-success.json 中的 path。
sudo sh -c 'cd "<备份目录>" && sha256sum --check SHA256SUMS'
```

成功的 oneshot 服务显示 `inactive (dead)` 是正常状态；应检查 `Result=success`、
退出码为 0、最新 `last-success.json` 时间，以及 timer 是否为 `active (waiting)`。
备份失败不会更新上一次成功时间。此配置记录失败并自动重试，不发送外部通知。
可用 `sudo systemctl start annotation-backup.service` 手动补做一次备份。

## 恢复

先校验 `SHA256SUMS`，再使用 PostgreSQL 18 的 `pg_restore` 恢复至**新建空库**：

```bash
/usr/lib/postgresql/18/bin/pg_restore --exit-on-error --single-transaction \
  --no-owner --no-acl --dbname='<空的恢复目标库连接串，不含密码>' '<备份目录>/production.dump'
```

使用目标库的迁移 owner 连接，凭据通过受限的 `.pgpass` / `PGPASSFILE` 提供；
恢复后按应用部署要求设置 app/backup 角色权限，核对 `verification.json` 的
迁移版本和表行数。不要把线上生产库作为演练目标。

## 2026-09-21 上线验证

- 已安装并启用 `annotation-backup.timer`，下一次计划执行为北京时间
  2026-09-22 03:17；配置为开机启用。
- 首次生产库备份于北京时间 11:20:49 完成，约 74 MB，耗时约 26 秒。
  PostgreSQL 18 完整恢复成功，报告覆盖 27 张业务表、迁移版本 1–7；
  `SHA256SUMS` 全部通过。
- 备份角色具有所有 public 业务表的 SELECT 权限，无 INSERT / UPDATE /
  DELETE / TRUNCATE 权限；生产应用在执行期间保持运行。
- `python3 -m unittest discover -s tests -p test_production_backup.py -v`：
  2 项测试通过，覆盖过期清理边界，以及恢复失败时不发布备份、不清理旧备份。
