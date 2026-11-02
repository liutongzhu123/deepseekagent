# P1-1 数据层：PostgreSQL + MinIO

> 状态：代码、迁移/对账/回滚脚本、备份恢复脚本与自动化测试已完成；
> 文件模式仍是默认值，切换由环境变量控制。本文记录设计、运行手册、验证证据与限制。

## 1. 本轮做了什么

P0 的用户、审计、标准（含复核修订链）、报价原来全部是 `data/` 下的 JSON/JSONL/文件。
本轮增加一层可切换的数据层：

| 能力 | 实现 |
|---|---|
| 关系数据 | PostgreSQL：用户、审计事件、标准、标准版本、条款、报价、文件元数据 |
| 解析结果 | `standards.data` 用 JSONB 保存，API 字段完全不变；每次保存写一条 `standard_versions` 快照 |
| 原件/导出 | 标准原件、标准 Excel、报价 CRM 草稿/销售报价表进对象存储（MinIO 或本地目录） |
| 审计 | `audit_events` 表，按时间/用户/事件/路径/状态过滤，不再全量扫 JSONL；保留期清理走 SQL |
| 迁移 | `scripts/migrate_to_postgres.py`：导入 + 对账，幂等可重跑，不删除源文件 |
| 回滚 | 迁移期双写 JSON 镜像；`scripts/rollback_to_files.py --check` 校验，`--export` 可从数据库重建文件树 |
| 备份 | `scripts/backup_store.py`：`pg_dump` + 对象存储 tar + SHA-256 清单；`restore_store.py` 校验并恢复 |
| 测试 | 文件模式原有 73 项 + PostgreSQL/迁移/回滚 4 项集成测试；CI 增加 PostgreSQL service |

没有改前端、没有改 API 路径/字段，也没有把报告审核历史、CRM 实体、异步任务表提前建进来。

## 2. 架构

```
FastAPI (app/main.py)
  ├─ auth.py ───────────────┐
  ├─ standard_store.py ─────┤  P0_STORE=json   -> data/ 下原 JSON/文件
  ├─ quote_store.py ────────┤  P0_STORE=postgres -> app/store/*
  └─ main.py 导出/下载 ─────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │ PostgreSQL                   │ 对象存储
                    │ users / audit_events         │ local: data/objects/
                    │ standards / versions/items   │ minio: P0_MINIO_*
                    │ quotes / files               │
                    └──────────────────────────────┘
```

- `P0_STORE` 在每次调用时读取，测试可以切换而不重启。
- `P0_STORE=postgres` 时仍写 JSON 镜像（`P0_STORE_MIRROR=1`，默认开），
  所以回滚通常只是把 `P0_STORE` 改回 `json` 并重启。
- 对象键：`<owner_type>/<owner_id>/<file_id>-<safe_name>`；`files` 表记录
  名称、对象键、大小、SHA-256、内容类型，迁移对账按哈希比较。

### 表

| 表 | 说明 |
|---|---|
| `users` | 用户名主键；角色/启用状态建索引；完整用户文档在 JSONB，密码哈希单列 |
| `audit_events` | 审计事件；`source/source_seq` 记录迁移来源，重跑不重复；索引 `at/user/event` |
| `standards` | 标准主记录；`project/created_by/standard_no/name/published` 建索引；完整文档 JSONB |
| `standard_versions` | `(standard_id, version)` 唯一；每次修订保存快照 |
| `standard_items` | 条款级窄表，便于后续报表查询；JSONB 文档仍是 API 的事实来源 |
| `quotes` | 报价元数据；项目/创建人/时间 |
| `files` | 所有对象的元数据；`(owner_type, owner_id, name)` 唯一，重复导出会替换旧对象 |

`projects` / `project_members` 暂不建表：当前应用没有项目 CRUD，项目是标准/报价/用户授权上的字符串标签；
等 P1-4/P1-5 引入项目实体时再建，避免空表假装实现。

## 3. 配置

```bash
P0_STORE=json|postgres                 # 默认 json
P0_DATABASE_URL=postgresql+psycopg://p0:<password>@127.0.0.1:5432/p0
P0_OBJECT_STORE=local|minio            # 默认 local
P0_OBJECT_DIR=/opt/cq-ai-p0/data/objects   # local 模式
P0_MINIO_ENDPOINT=127.0.0.1:9000
P0_MINIO_ACCESS_KEY=...
P0_MINIO_SECRET_KEY=...
P0_MINIO_BUCKET=cq-p0
P0_MINIO_SECURE=0                      # 公网/HTTPS 时设 1
P0_STORE_MIRROR=1                      # 迁移期保持 1；切换完成后才可关
```

数据库账号只需要目标库的 DDL/DML 权限；服务启动时 `ensure_schema()` 会补建缺失表。
生产建议单独建库与账号：

```sql
CREATE ROLE p0 LOGIN PASSWORD '<strong-password>';
CREATE DATABASE p0 OWNER p0;
```

## 4. 迁移运行手册

> 前提：先完成 P1-0.5 的文件快照，并确认恢复演练可用。迁移期间停服务。

1. 文件快照（现有脚本，保留 JSON 回滚点）：

   ```bash
   .venv/bin/python scripts/backup_data.py \
     --data-dir /opt/cq-ai-p0/data --backup-dir /var/backups/cq-ai-p0 --keep 14
   ```

2. 预检（不写库）：

   ```bash
   P0_DATABASE_URL=... .venv/bin/python scripts/migrate_to_postgres.py --dry-run
   ```

3. 停服务并迁移：

   ```bash
   systemctl stop cq-p0-api
   P0_DATABASE_URL=... P0_OBJECT_STORE=minio ... \
     .venv/bin/python scripts/migrate_to_postgres.py \
       --data-dir /opt/cq-ai-p0/data --report /tmp/p0-migrate.json
   ```

   脚本会导入用户、审计、标准（含原件/导出/kb）、报价，然后自动对账。
   成功输出 `reconciliation: OK`，失败时退出码 1 并列出差异。

4. 只对账（可重复执行）：

   ```bash
   P0_DATABASE_URL=... .venv/bin/python scripts/migrate_to_postgres.py --verify-only
   ```

5. 切换并启动：

   ```bash
   # .env 增加
   P0_STORE=postgres
   P0_DATABASE_URL=...
   P0_OBJECT_STORE=minio
   P0_MINIO_*...
   P0_STORE_MIRROR=1
   systemctl start cq-p0-api
   curl -s http://127.0.0.1:8090/health   # store=postgres
   ```

6. 冒烟：登录、标准列表/详情/导出、报价下载、审计查询、知识问答各一次。
   迁移脚本不改变 API 字段；导出/下载改为 `Response`，文件名保持原样。

### 对账口径

- 用户：用户名集合相等 + 每个用户文档逐字段相等；
- 审计：JSONL 行与数据库记录（按排序后的 JSON 文档）多重集合相等；
- 标准：ID 集合相等、`result.json` 与数据库文档相等、`kb.json` 相等、
  `source.pdf` / `export.xlsx` 与对象存储内容 SHA-256 相等；
- 报价：ID 集合相等、`meta.json` 相等、文件名集合相等、每个文件 SHA-256 相等。

### 回滚

迁移期双写保证通常不需要导出；直接改配置：

```bash
systemctl stop cq-p0-api
# .env 删除 P0_STORE / P0_DATABASE_URL / P0_OBJECT_STORE / P0_MINIO_*
systemctl start cq-p0-api
```

若迁移后仍有写入且镜像未开，先用数据库重建文件树：

```bash
.venv/bin/python scripts/rollback_to_files.py --check     # 先看差异
.venv/bin/python scripts/rollback_to_files.py \
  --export /opt/cq-ai-p0/data.rollback
# 停服后把 data 移开、把 data.rollback 改成 data，再以 P0_STORE=json 启动
```

`docs/p1-ci-backup/README.md` 的恢复流程仍适用于文件模式；数据库/对象模式使用下一节。

## 5. 备份与恢复

每日文件快照保留不变；数据层再加一份：

```bash
P0_DATABASE_URL=... P0_MINIO_*... \
  .venv/bin/python scripts/backup_store.py \
    --backup-dir /var/backups/cq-ai-p0 --keep 14
```

产物：

- `p0-pg-<UTC>.dump`：`pg_dump --format=custom`；
- `p0-objects-<UTC>.tar.gz`：所有对象 + `manifest.json`（键/大小/SHA-256）；
- `p0-store-<UTC>.json`：两个归档的大小/SHA-256 与数据库地址（已脱敏密码）。

恢复（会覆盖同名表与对象，先停服并确认清单）：

```bash
.venv/bin/python scripts/restore_store.py \
  --backup-dir /var/backups/cq-ai-p0 --verify-only
systemctl stop cq-p0-api
.venv/bin/python scripts/restore_store.py \
  --backup-dir /var/backups/cq-ai-p0 --yes
systemctl start cq-p0-api
```

`deploy/p0-store-backup.service` / `.timer` 是 03:40 的定时单元示例；安装前把
`P0_DATABASE_URL` 与 MinIO 凭据放入 `/opt/cq-ai-p0/.env`。归档含客户材料与账号哈希，
权限 0600，必须异机保存。

## 6. 验证证据（2026-11-02，本地）

- 文件模式全量回归：`73 passed, 2 skipped`；
- 启用 PostgreSQL service 后全量：`77 passed, 2 skipped`；
- 迁移演练（合成 1 用户 / 2 审计 / 1 标准 / 1 报价）：
  `imported.errors=[]`，`users=1, audit_events=2, standards=1, quotes=1`，
  `reconciliation: OK`；
- 备份演练：`pg_dump` 16,912 B + 对象 tar 3 objects；`restore_store.py --verify-only` 通过；
  恢复到新库后计数 `1|2|1|1|3`，对象 3 个；
- 回滚演练：`rollback_to_files.py --check` 无差异；`--export` 生成完整 JSON 树，
  `P0_STORE=json` 打开同一棵树可读（集成测试断言）。
- PostgreSQL 版本：本地 PostgreSQL 14；CI 使用 PostgreSQL 16 service；
- MinIO 客户端路径用 S3 兼容测试服务（moto）实测了 put/get/exists/list/delete 与
  bucket 自动创建逻辑（bucket 需预先创建时由部署脚本完成）。

## 7. 限制与未完成项

- **未部署到共享服务器**：本工单没有服务器 SSH 凭据，无法执行生产切换；
  以上演练在本地 PostgreSQL 14 + 本地对象存储完成。部署前需人工提供部署窗口与凭据。
- **生产 MinIO 未安装/未切换**：客户端路径已用 S3 兼容测试服务验证，但共享服务器上
  还没有 MinIO 实例；本地与 CI 主要用 `P0_OBJECT_STORE=local` 覆盖文件元数据/哈希/回滚。
  上线前需在服务器起 MinIO（或内部 S3）、预建 bucket，并跑一次迁移对账。
- **项目实体表未建**（见 §2）；P1-5 的 CRM 样例导入不在本增量。
- **报告审核历史未落库**：按计划留到 P1-4；本轮只迁移已有的标准/报价/审计/用户。
- 迁移脚本假设停服务执行；双写镜像降低但未消除并发写入的竞态。
- 审计过滤在数据库侧完成，但 `query_audit` 的 CSV 导出仍按筛选结果读入内存后再写 CSV；
  百万级审计需要后续加导出流式化。
