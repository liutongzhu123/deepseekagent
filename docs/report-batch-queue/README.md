# 批量审核接任务队列：前端进度与默认启用（T303b 收尾）

> 状态：2026-11-06。基线 `main` @ `bab83a9`（PR #66 的后端入队已合并）。
> 范围：只补批量审核的异步前端路径与开关，不改审核规则、批量响应结构或同步路径。

## 1. 背景

- PR #66 给 `POST /api/v1/reports/batch` 加了入队分支：任务队列可用时返回
  `202 {task_id, status_url}`。但当时开关默认关闭，前端也不认识 202。
- 本增量：前端收到 202 后轮询 `/api/v1/tasks/{id}`，显示阶段与进度，完成后渲染同一份
  批量结果（`items`/`summary` 结构不变）；队列可用时默认走异步。

## 2. 改动

- `p0/static/ui.js`：新增 `waitForReportTask`；批量提交识别 `task_id`；阶段中文化
  （排队/读取文件/审核中/保存结果）；失败、取消、超时给出明确错误。
- `p0/app/main.py`：`P0_TASKS_REPORT_BATCH_ASYNC` 默认值由 `0` 改为 `1`；未启用任务队列
  （`P0_TASKS_ENABLED` 关闭或非 PostgreSQL）时仍走原同步路径。
- 测试：`test_store_postgres.py` 新增队列回环用例（入队 → 对象存储 → worker → 聚合结果）；
  `test_ui_reports.py` 覆盖前端轮询标记。

## 3. 验证

- 本地 JSON 模式：`cd p0 && python -m pytest -q` → **229 passed, 23 skipped**。
- 本地 PostgreSQL 15.19（UTF-8 临时实例，`P0_TEST_DATABASE_URL`）：
  `pytest tests/test_store_postgres.py -q` → **11 passed**，含新增的队列回环。
- 浏览器实测（1512 宽、浅色主题、同一份 2 个合成 docx，真实 PostgreSQL + worker）：
  - 同步基线：提交后直接出现批量结果与「完成：2 份成功」；
  - 异步路径：提交后显示「后台审核中：排队 0%」，完成后结果与同步一致，审核历史写入 2 条。

| 截图 | 内容 |
|---|---|
| `before-batch.png` | 基线（PR #66，默认同步）：提交后直接出批量结果 |
| `after-queued.png` | 本增量：202 入队后显示「后台审核中：排队 0%」 |
| `after-batch.png` | 本增量：轮询完成后渲染批量结果（与同步一致） |

## 4. 限制

- 异步只在 `P0_STORE=postgres` 且 `P0_TASKS_ENABLED=1` 时生效；worker 未运行时任务会排队，
  前端轮询 150 次（约 3.5 分钟）后提示超时并建议到审核历史查看。
- 批量任务的取消/重试只有 API（`/api/v1/tasks/{id}/cancel|retry`），UI 未加按钮。
- 进度是每文件粒度的粗阶段（排队/读取/审核/保存），不是逐条规则进度。
- 截图使用合成 docx，不含客户内容。
