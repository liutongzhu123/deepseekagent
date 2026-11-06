# 审核历史分页/搜索与报告保留清理（T303）

> **状态**：2026-11-06。基线 `main` @ `c1511b0`（阶段 3 准备文档 PR #63 合并后；PR #64 尚未合并）。
> **范围**：只补报告审核历史的使用缺口与留存清理，不改审核 API、规则判定、存储格式或项目隔离。
> **对应计划**：`docs/stage3-acceptance/next-plan.md` 的 T303；批量审核接任务队列仍未纳入本增量。

## 1. 背景

- 报告审核历史接口 `/api/v1/reports` 早已支持 `limit`/`offset`/`q`/`batch_id`，但工作台只取一次
  `limit=20`，第 21 条以后看不到，也无法按文件名/项目检索。
- 原文与结果一直按 `MAX_UPLOAD_MB`（默认 20 MB/份）留存；历史增长后没有清理策略，文件模式会持续占用磁盘，
  PostgreSQL 模式会同时留下 `report_audits` 行、对象存储原文与 JSON 镜像。

## 2. 审核历史分页与搜索

- 历史卡片新增搜索框（文件名/项目，走既有 `q` 参数）与「上一页 / 下一页」、总数与页码；单页 20 条。
- 审核完成或批量审核完成后回到第 1 页；打开历史记录不会改变当前页。
- 空态区分「还没有审核记录」与「没有匹配的审核记录」。
- 项目隔离与权限不变：仍调用 `/api/v1/reports`，服务端按 `report:read` + 项目过滤。

涉及：`p0/static/index.html`、`p0/static/ui.js`、`p0/static/ui.css`。

## 3. 报告保留清理

- `p0/app/report_store.py` 新增 `prunable_ids(cutoff)` / `prune_before(cutoff)`：

  - 文件模式（默认）：删除 `P0_REPORT_DIR/<id>/` 整个目录（meta/result/source/reviews）；
  - PostgreSQL 模式：删除 `report_audits` 行、对象存储中的原文、以及 JSON 镜像目录；
  - **缺少 `created_at` 的记录不会被删除**（无法判断年龄，宁可保留）。

- 新增 `p0/scripts/prune_reports.py`：

  ```bash
  cd p0
  python scripts/prune_reports.py --days 180          # 默认 dry-run，只统计
  python scripts/prune_reports.py --days 180 --apply  # 删除并写 reports_pruned 审计
  ```

  `--days` 必须 ≥ 1；脚本不提供 HTTP 入口，也不自动执行，避免误删。自动清理需要运维加 timer
  （与 `scripts/prune_tasks.py` 同一模式）。

- 存储边界不变：仍然只存审核结果与原文，不新增客户数据字段。

## 4. 验证

- 新增 `p0/tests/test_report_retention.py`（4 条）：cutoff 边界与缺失时间戳、目录删除、脚本 dry-run/apply、
  非法 `--days`。
- `p0/tests/test_ui_reports.py` 新增历史搜索/分页控件检查。
- `p0/tests/test_store_postgres.py` 新增 `test_report_prune_removes_old_audits`（无 `P0_TEST_DATABASE_URL` 时跳过）。
- 本地全量：`cd p0 && python -m pytest -q` → **224 passed, 22 skipped**（Python 3.11；CI 为 3.12）。
- 浏览器实测：同一 1512 宽视口、浅色主题、同一份 25 条历史记录（假数据，不含客户内容），基线应用为
  `main` @ `c1511b0`，改动应用为本分支：

  | 截图 | 内容 |
  |---|---|
  | `before-history.png` | 基线：审核历史只取前 20 条，无搜索/分页；共 25 条但只能看到 20 条 |
  | `after-history.png` | 本增量：搜索框 + 共 25 条、显示第 1–20 条、上一页/下一页（1/2） |
  | `after-search.png` | 本增量：搜索「气味报告-2」后共 6 条（1512×838 单屏） |

  截图是整页（before/after 同宽同数据）与单屏（搜索）两种；没有使用客户报告，历史记录为合成假数据。

## 5. 未纳入本轮

- **批量审核接 P1-2 任务队列**：当前批量仍在请求内顺序执行，受 `P0_REPORT_BATCH_MAX`（默认 20）限制；
  接队列需要返回 202 + 任务进度，涉及 `main.py` / `task_worker.py`，留作后续增量。
- 保留期自动调度（systemd timer）与保留策略的产品口径（多少天）需要运维/项目负责人确认；脚本默认可先手动跑。
- 审核历史未做按日期/批次筛选（接口已支持 `batch_id`，UI 未加控件）。
