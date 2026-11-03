# 审计导出流式化与任务保留清理（0.8.3 收尾）

> 目的：关掉 P1-1/P1-2 文档里明确记的两个生产化尾巴——百万级审计导出会把结果
> 全部读进内存，以及已完成的后台任务没有清理策略。不改 API 路径与 CSV 格式。

## 1. 审计 CSV 流式导出

`GET /api/v1/audit/export.csv` 现在返回 `StreamingResponse`：

- PostgreSQL 模式：`store.audit.iter_entries()` 用 `stream_results=True` 逐行读取，
  CSV 边读边写，不再先构造完整 `events` 列表；
- 文件模式：仍是先过滤 `audit.jsonl`（单文件顺序读），本次没有改文件存储格式；
- 输出与之前一致：UTF-8 BOM + 表头 + 行（`audit_csv` 仍保留给测试/离线使用）；
- `audit_exported` 审计事件仍记录筛选后的 `rows`（通过 `count_audit` 统计，不加载全部行）。

筛选条件（用户/角色/事件/方法/路径/状态/日期）与权限（`audit:export`，admin）不变。

## 2. 任务保留清理

`scripts/prune_tasks.py` 只删除**已完成**且超过保留期的任务，`queued/running` 永不删除：

```bash
cd /opt/cq-ai-p0
# 先看会删多少（默认 dry-run）
.venv/bin/python scripts/prune_tasks.py --days 30
# 真正删除成功/失败/取消且 finished_at 早于 30 天前的任务
.venv/bin/python scripts/prune_tasks.py --days 30 --apply
# 只清失败/取消
.venv/bin/python scripts/prune_tasks.py --days 90 --status failed,cancelled --apply
```

- 仅支持 `P0_STORE=postgres`（任务队列本来就在 PostgreSQL）；文件模式会直接报错；
- `--apply` 成功后写 `tasks_pruned` 审计事件（rows、days、操作人 system）；
- 可放到 systemd timer/cron（例如每周一次 dry-run 观察，再按月 apply），
  服务器上的具体 timer 仍需运维安装，本轮不自动改服务器。

## 3. 验证

```bash
cd p0
pytest -q
# 带 PostgreSQL
P0_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/p0_test pytest -q
```

覆盖：

- `tests/test_retention.py`：流式 CSV 与内存 CSV 完全一致、空导出只有 BOM+表头、
  `'=cmd` 注入防护仍在；
- `tests/test_store_postgres.py::test_task_prune_removes_only_old_finished`：
  40 天前完成的任务被删、新完成任务保留、queued 状态不会被误删、未知状态报错；
- 既有 `test_p1_identity::test_audit_query_and_export` 继续覆盖导出接口与权限。

结果：带 PostgreSQL 全量 `146 passed, 2 skipped`（本地）。

## 4. 限制

- 文件模式审计仍是整体读 JSONL 后逐行输出；百万级审计应使用 PostgreSQL 模式；
- 任务清理是手动/定时脚本，不提供 HTTP 入口，也不自动按审计保留期执行；
- 审计保留期仍由 `P0_AUDIT_RETENTION_DAYS` 的既有清理逻辑负责，两者独立；
- 清理不可恢复；先 dry-run 并确认备份策略。
