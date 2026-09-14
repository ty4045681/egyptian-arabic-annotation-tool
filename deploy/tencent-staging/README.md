# 腾讯云测试部署记录

部署日期：2026-09-14。服务器：`ubuntu@43.165.0.67`。

## 当前状态

- Ubuntu 26.04，4 vCPU，约 8GB RAM，系统盘约 118GiB。
- Python 3.12.14（uv 管理），uv 0.12.13，PostgreSQL 18.6，Nginx 1.28.3。
- 已部署当前工作区的运行文件，包括未提交修改。没有复制真实配置、标注数据库或音频。
- `deployment-code-manifest.json` 在云端项目目录记录源提交及 21 个运行文件的 SHA-256；源提交本身不代表未提交修改。
- 独立数据库 `annotation_tool_staging`，schema migrations `[1, 2]`。
- 测试数据：8 条 6 秒合成音频，全部标记为排除训练。部署验收完成了其中 1 条，剩余 7 条可领取。
- Nginx 仅监听本机 8080，Gunicorn 仅监听本机 8081，PostgreSQL 仅监听本机 5432。
- `annotation-staging`、Nginx、PostgreSQL 已设置开机启动。

## 在自己的电脑访问

使用能登录此服务器的 SSH 配置/密钥，保持命令运行：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:8080 ubuntu@43.165.0.67
```

若密钥不在 SSH 默认位置，请加 `-i /你的密钥路径`。

- 标注页面：<http://127.0.0.1:18080/login.html>
- 管理员页面：<http://127.0.0.1:18080/admin>
- 健康检查：<http://127.0.0.1:18080/api/health>

标注页面输入一个固定测试用户名即可。管理员密钥只保存在云端 root 可读文件中，按需读取：

```bash
ssh ubuntu@43.165.0.67 'sudo cat /root/annotation-staging-admin-key.txt'
```

当前是经 SSH 加密隧道访问的 HTTP 测试环境，管理员 Cookie 使用非 `__Host-` 名称且 Secure=false。
正式域名 HTTPS 上线时需切换 Secure=true 和 `__Host-` Cookie 名称，调整 Nginx 监听及证书。

## 云端路径

| 内容 | 云端路径 |
|---|---|
| 代码及虚拟环境 | `/opt/annotation_tool` |
| uv 管理的 Python | `/opt/annotation-python` |
| 应用配置 | `/opt/annotation_tool/config.json` |
| 测试库 app 连接及 Admin 配置 | `/etc/annotation-staging.env`（root 0600） |
| 测试库 owner 连接 | `/etc/annotation-staging-owner.env`（root 0600） |
| Admin 明文密钥 | `/root/annotation-staging-admin-key.txt`（root 0600） |
| 合成音频 | `/data/annotation/staging-audio` |
| Nginx 配置 | `/etc/nginx/sites-available/annotation-staging` |
| systemd 服务 | `/etc/systemd/system/annotation-staging.service` |
| 浏览器错误日志 | `/var/log/annotation-staging/client_errors.log` |

服务使用无登录权限的 `annotation` Linux 用户，数据库使用独立 owner/app 角色。
应用日志通过 journald 管理；项目的 client_errors.log 为日志目录中的文件链接。
新安装 Nginx 的默认站点链接已移至 `/etc/nginx/default-site.disabled`。

## 运维和验收

以下命令在云端执行：

```bash
sudo systemctl status annotation-staging --no-pager
sudo journalctl -u annotation-staging -n 100 --no-pager
curl -fsS http://127.0.0.1:8080/api/health
sudo systemctl restart annotation-staging
```

重新执行端到端测试（会消耗一条未完成的合成任务，并重启测试服务）：

```bash
cd /opt/annotation_tool
sudo env UV_PYTHON_INSTALL_DIR=/opt/annotation-python \
  UV_CACHE_DIR=/var/cache/annotation-provision \
  /usr/local/bin/uv run --no-sync python deploy/tencent-staging/smoke_http.py
```

已验证：Nginx/systemd 配置、schema、登录和页面响应、领取、保存幂等性、旧 revision 冲突、
重启及退出登录后的任务恢复、完成及个人历史、管理员登录/查询/CSRF 注销、
音频文件内容/206 Range/波形、未登录音频访问和直接 internal 路径被拒绝。
这些是实际 HTTP 接口检查；浏览器播放听感和交互仍由人工验收。

更新代码时只传代码文件，保留云端 `.venv`、`config.json` 和 `/etc/annotation-staging*.env`。
依赖按 `uv.lock` 使用 `uv sync --frozen --no-dev` 更新；迁移用 owner 环境执行，运行用 app 环境。
本目录配置替代原仓库中写死本机用户名、路径及 Cloudflare Tunnel 的部署模板。

## 正式迁移前待处理

1. **容量**：本地 `/home/ck/ar_audios` 约 166GiB；云端部署前剩余约 107GiB，无法容纳全量音频。
   需扩容或另挂数据盘，容量还要覆盖数据库、增长和备份；建议按至少 300GB 数据盘评估，最终以实测为准。
2. 确定正式访问域名、HTTPS、腾讯云安全组。调试不需要开放 80/443/8080/8081/5432。
3. 在新建正式库中恢复最终备份，不能把测试数据合并进去。
4. 源端当前 PostgreSQL 客户端为 16.15，云端为 18.6。计划用源端匹配版本的 pg_dump 导出、
   云端 pg_restore 恢复；正式切换前需做一次真实数据恢复演练和校验。
   PostgreSQL 官方支持将 dump 导入较新的版本，但不保证反向恢复到旧版本：
   <https://www.postgresql.org/docs/18/app-pgdump.html>。
5. 正式切换时停止所有源端写入及自动拉起监控，做最终数据库快照和音频增量同步。
   云端恢复、授权、schema 检查及数据校验通过后再切换入口，旧站保持停止写入。
6. 云端产生新标注后，回退必须处理这些新增数据，不能直接把入口指回旧库。

本次仅完成云端测试部署，没有停止本地网站、迁移正式数据库、复制真实音频或改变用户入口。
