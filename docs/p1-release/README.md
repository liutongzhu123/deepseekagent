# P1 版本化发布包与割接/回滚手册

> 目的：把「合并到 main」变成「可重复、可校验、可回滚地上线」。
> 当前主线版本 **0.8.3**；生产仍可能落后，部署需要服务器访问权限与维护窗口。
> 本文档与 `scripts/release_kit.py` 不修改应用行为，也不在服务器上自动切换服务。

## 1. 发布包

`p0/scripts/release_kit.py` 打包 `app/`、`static/`、`tests/`、`scripts/`、`deploy/`、
`requirements.txt`、`README.md`，并写 `manifest.json`（版本、Git commit、每个文件的
大小与 SHA-256）与 `.sha256` sidecar。

```bash
cd p0
.venv/bin/python scripts/release_kit.py build --out /tmp/releases
# 产物：/tmp/releases/cq-ai-p0-<version>.tar.gz + .sha256
.venv/bin/python scripts/release_kit.py verify --bundle /tmp/releases/cq-ai-p0-<version>.tar.gz
```

`verify` 会检查：

- sidecar SHA-256 与包一致；
- 包内 `manifest.json` 存在，所有文件的大小/哈希与 manifest 一致；
- 不包含符号链接或路径穿越；
- 必需的 `app/main.py`、`static/index.html`、`requirements.txt` 存在。

包结构：

```
p0-<version>/
  app/ static/ tests/ scripts/ deploy/
  requirements.txt  README.md  manifest.json
```

## 2. 上线前检查（服务器）

```bash
cd /opt/cq-ai-p0
# 当前服务、磁盘、备份、证书（按环境加 --tls-host）
.venv/bin/python scripts/monitor_check.py --base-url http://127.0.0.1:8090 \
  --metrics-token "$P0_METRICS_TOKEN" --data-dir /opt/cq-ai-p0/data \
  --backup-dir /var/backups/cq-ai-p0 --strict
# 代码/数据备份（两份都要；数据库模式再加 pg_dump）
mkdir -p /var/backups/cq-ai-p0
tar czf /var/backups/cq-ai-p0/code-before-$(date +%Y%m%dT%H%M%S).tar.gz \
  -C /opt/cq-ai-p0 app static tests scripts requirements.txt README.md
.venv/bin/python scripts/backup_data.py --data-dir /opt/cq-ai-p0/data \
  --backup-dir /var/backups/cq-ai-p0 --keep 14
# P0_STORE=postgres 时：
.venv/bin/python scripts/backup_store.py --backup-dir /var/backups/cq-ai-p0 --keep 14
```

检查清单：

- [ ] `/health` 为 `status=ok`，版本是**当前**线上版本；
- [ ] 备份文件存在、可读，`restore_* --verify-only` 通过；
- [ ] 服务器磁盘余量 ≥ 20%，`/var/backups/cq-ai-p0` 可写；
- [ ] 有回滚窗口（建议 ≥ 30 分钟）和能登录服务器的操作人；
- [ ] 生产 `P0_ENV=production`、`P0_WEKNORA_REQUIRED=1`、`P0_METRICS_TOKEN` 已配置。

## 3. 割接步骤

1. 本地构建并校验发布包，记录 `sha256`：

   ```bash
   cd p0 && .venv/bin/python scripts/release_kit.py build --out /tmp/releases
   .venv/bin/python scripts/release_kit.py verify --bundle /tmp/releases/cq-ai-p0-0.8.3.tar.gz
   ```

2. 传到服务器（示例）：

   ```bash
   scp /tmp/releases/cq-ai-p0-0.8.3.tar.gz* root@<P0_HOST>:/opt/releases/
   ```

3. 在服务器解包并只替换代码目录（**保留** `.venv`、`.env`、`data/`）：

   ```bash
   cd /opt/cq-ai-p0
   mkdir -p /opt/releases && cd /opt/releases
   sha256sum -c cq-ai-p0-0.8.3.tar.gz.sha256
   tar xzf cq-ai-p0-0.8.3.tar.gz
   cd /opt/cq-ai-p0
   cp -a /opt/releases/p0-0.8.3/{app,static,tests,scripts,requirements.txt,README.md} .
   ```

4. 在服务器上跑回归，再重启：

   ```bash
   cd /opt/cq-ai-p0
   .venv/bin/python -m pytest -q tests
   systemctl restart cq-p0-api
   systemctl status --no-pager cq-p0-api | head -5
   ```

5. 发布后校验（版本必须匹配）：

   ```bash
   cd /opt/cq-ai-p0
   .venv/bin/python scripts/release_kit.py check \
     --base-url http://127.0.0.1:8090 --expect-version 0.8.3 \
     --metrics-token "$P0_METRICS_TOKEN" --strict
   # 再用公网入口跑一次（可加 --metrics-token）
   .venv/bin/python scripts/release_kit.py check \
     --base-url https://<DOMAIN>/p0 --expect-version 0.8.3 --strict
   ```

6. 业务冒烟：管理员登录 → 五个页签 → 报告审核一份 → 标准列表/复核 → 报价下载 →
   知识问答一条 → 审计查询；记录结果与截图。

## 4. 回滚

任何一步失败，先停服务再恢复代码目录（数据目录不动）：

```bash
systemctl stop cq-p0-api
cd /opt/cq-ai-p0
tar xzf /var/backups/cq-ai-p0/code-before-<timestamp>.tar.gz -C /opt/cq-ai-p0
systemctl start cq-p0-api
.venv/bin/python scripts/release_kit.py check \
  --base-url http://127.0.0.1:8090 --expect-version <旧版本> --strict
```

- 只回滚代码时不需要恢复 `data/`；若代码改动涉及数据层且已执行迁移，按
  `docs/p1-data-layer/README.md` 的 `rollback_to_files.py` 走数据回滚。
- 回滚后补一条事故记录：版本、失败步骤、日志路径、恢复耗时。

## 5. 验证与限制

- 单测：`tests/test_release_kit.py` 覆盖 build/verify、sidecar/内容篡改、`/health` 版本与
  `auth` 校验、`/metrics` 403 跳过；
- 发布包在本地构建并校验（当前 0.8.3，107 个文件，≈284 KB）；
- **本工单没有服务器 SSH 权限**：割接/回滚步骤由有权限的运维按本文执行；
  脚本不自动切换 systemd 服务，也不替人决定维护窗口；
- 发布包包含 `tests/`，服务器跑 `pytest tests` 需要对应依赖已安装在 `.venv`；
- 生产仍使用单机 `systemd + nginx`；蓝绿/灰度未实现，回滚以代码目录快照为准。
