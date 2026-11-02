# P1-0.5 · CI 与文件数据备份基线

> 基线：`main` @ `50bdb95`（0.6.0 计划合并后）。对应 `docs/p1-next-plan.md` 的 P1-0.5。
> 本增量只补“数据迁移前的安全网”：CI 跑 pytest、文件数据快照/校验/恢复脚本、每日快照定时器。
> 不改 API、数据结构、前端，不迁移历史数据；生产 0.6.0 发布（P1-0）仍是独立运维步骤。

## 1. 改动

| 文件 | 作用 |
|---|---|
| `.github/workflows/p0-ci.yml` | push `main` / PR 到 `main` 时在 Ubuntu + Python 3.12 上跑 `p0` 的 pytest；`workflow_dispatch` 可手动触发；同分支并发取消旧任务 |
| `p0/scripts/backup_data.py` | 对 `P0_DATA_DIR` 生成带逐文件 SHA-256/size/mode 清单的 `tar.gz`，`0600`，原子落盘，按数量保留 |
| `p0/scripts/restore_data.py` | 校验 archive 与清单、拒绝路径穿越、先解压到临时目录再替换；非空目标必须 `--force`，旧目录保留为 `*.pre-restore-<时间戳>` |
| `p0/tests/test_backup_restore.py` | 备份→恢复→用恢复目录启动应用并读标准列表；拒绝非空目标、篡改检测、路径穿越、保留策略 |
| `p0/deploy/p0-backup.service` / `.timer` | 服务器每日 03:30 本地时间快照（`Persistent=true`，错过会补跑） |
| `p0/README.md`、`p0/deploy/README.md`、`p0/.gitignore` | 命令、备份目录忽略 |

备份内容就是 `P0_DATA_DIR` 下的全部普通文件：`auth/users.json`、`auth/session_secret`、
`audit/audit.jsonl`、`standards/<id>/source.pdf|result.json|kb.json`、`quotes/<id>/`。
符号链接、设备/FIFO、`*.tmp` 会跳过并告警，避免把数据目录外的内容或写了一半的临时文件带进快照。

## 2. CI

触发条件：

- push 到 `main` 且改动 `p0/**` 或本 workflow；
- PR 到 `main` 且改动同上；
- 手动 `workflow_dispatch`。

工作流执行 `pip install -r p0/requirements.txt` 后 `cd p0 && python -m pytest -q`。
本地等价命令：

```bash
cd p0
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

`p0/tests/conftest.py` 已把测试数据目录指向临时目录，并关闭登录、WeKnora 必选和 secure cookie；
CI 不使用生产 `data/`，也不发起外部模型/知识库请求。仓库没有配置自托管 runner，使用 GitHub 托管 runner。

## 3. 每日快照与异机保留

### 3.1 手动快照

```bash
cd /opt/cq-ai-p0
.venv/bin/python scripts/backup_data.py \
  --data-dir /opt/cq-ai-p0/data \
  --backup-dir /var/backups/cq-ai-p0 \
  --keep 14
```

输出示例（真实路径按环境变化）：

```
snapshot=/var/backups/cq-ai-p0/p0-data-20261102T193000Z.tar.gz
sha256=<64 hex>
files=1234 bytes=567890123
retention=14
```

默认值可用 `P0_DATA_DIR` / `P0_BACKUP_DIR` 覆盖；`--dry-run` 只列文件不写盘；
`--label before-migration` 可给迁移前的手工快照加标签。归档权限 `0600`，备份目录 `0700`。

### 3.2 安装每日定时器（服务器）

```bash
install -d -m 700 -o root -g root /var/backups/cq-ai-p0
install -m 644 p0/deploy/p0-backup.service /etc/systemd/system/
install -m 644 p0/deploy/p0-backup.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now p0-backup.timer
systemctl list-timers p0-backup.timer
# 先手动跑一次，确认输出和归档权限
systemctl start p0-backup.service
journalctl -u p0-backup.service -n 30 --no-pager
```

### 3.3 异机保留

`/var/backups` 与生产数据在同一台机器上，只能防误删/误改，不能防磁盘或整机故障。
必须把快照同步到另一台机器或独立存储，例如：

```bash
# 备份机上：/srv/backups/cq-ai-p0，接收账号只允许写该目录
rsync -a --delete-after /var/backups/cq-ai-p0/ backup@<BACKUP_HOST>:/srv/backups/cq-ai-p0/
# 每次同步后校验
sha256sum -c /var/backups/cq-ai-p0/p0-data-*.sha256
```

同步用独立定时器或备份机上的拉取任务，不与业务服务共用磁盘；保留至少 14 天，
迁移前额外保留一份带 `--label` 的快照。不要把快照提交到 Git，也不要放进 Web 可访问目录。

## 4. 恢复演练（迁移前必须做一次）

本地/备用机演练（不碰生产数据）：

```bash
cd /opt/cq-ai-p0
SNAP=/var/backups/cq-ai-p0/p0-data-20261102T193000Z.tar.gz

# 1) 先只校验，不写盘
.venv/bin/python scripts/restore_data.py "$SNAP" --verify-only

# 2) 恢复到临时目录
.venv/bin/python scripts/restore_data.py "$SNAP" --data-dir /tmp/p0-restore-check

# 3) 用恢复目录起一个临时实例
P0_DATA_DIR=/tmp/p0-restore-check \
P0_STANDARD_DIR=/tmp/p0-restore-check/standards \
P0_QUOTE_DIR=/tmp/p0-restore-check/quotes \
P0_AUDIT_LOG=/tmp/p0-restore-check/audit/audit.jsonl \
P0_WEKNORA_REQUIRED=0 \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8091

curl -s http://127.0.0.1:8091/health
```

`/health` 返回 `status=ok`，登录后能列出恢复前就存在的标准/报价，才算“从快照恢复出可用实例”。
`pytest -q tests/test_backup_restore.py` 已把这一流程自动化（启动应用、`GET /api/v1/standards`
校验恢复出的标准 ID）。

生产恢复（只在确认新版本有问题时执行）：

```bash
systemctl stop cq-p0-api
.venv/bin/python scripts/restore_data.py "$SNAP" --data-dir /opt/cq-ai-p0/data --force
systemctl start cq-p0-api
curl -s http://127.0.0.1:8090/health
```

`--force` 会把旧数据目录改名为 `data.pre-restore-<时间戳>`，不删除；快速回滚：

```bash
systemctl stop cq-p0-api
mv /opt/cq-ai-p0/data /opt/cq-ai-p0/data.bad
mv /opt/cq-ai-p0/data.pre-restore-<时间戳> /opt/cq-ai-p0/data
systemctl start cq-p0-api
```

目标：30 分钟内完成恢复或回滚。演练记录（日期、快照文件名、恢复耗时、检查项、执行人）
放在运维记录里，不要放进仓库。

## 5. 本次验证

- `pytest -q`：54 passed / 2 skipped（原有 49 + 新增 5；2 个跳过是未挂载真实样例的既有用例）；
- `pytest -q tests/test_backup_restore.py`：5 passed，含“恢复目录启动应用并读到标准”的集成校验；
- 手工演练：生成快照 → `--verify-only` → 恢复到临时目录 → `/health` 200 / 标准列表可见；
- 未在真实生产数据上执行恢复；每日定时器和异机同步需要运维在服务器安装（本 PR 只提供脚本、单元文件和步骤）。

## 6. 限制与不做

- 快照按文件原子写取副本，不是跨文件事务；高峰写入时可能取到相邻两个版本。安排在低峰执行，恢复后以清单校验和业务抽查为准；
- 备份归档包含账号哈希、会话密钥和客户标准/报价，归档 `0600`，异机存储必须同样限制访问，禁止外发；
- 恢复要求 Python 3.12+（使用 `tarfile.extractall(filter="data")`）；
- 定时器与异机同步的安装、生产恢复演练属于运维动作，本增量不自动改服务器；P1-0 的 0.6.0 发布仍需先完成；
- 不做数据库备份（P1-1 引入 PostgreSQL/MinIO 后再补 `pg_dump` 与对象存储镜像）；不做加密归档，依赖文件系统权限与异机访问控制；
- 不改 API、数据结构、前端，因此没有 Before/After 界面截图。

## 7. 回滚

本增量是新增文件，不改变运行时行为：

- 停用定时器：`systemctl disable --now p0-backup.timer`；
- 删除 workflow 或脚本不影响服务启动；
- 恢复数据时按 §4 使用 `*.pre-restore-*` 目录回滚；
- 若 CI 造成误报，先修测试或工作流，不要把失败测试改成跳过。
