# P1-5 CRM / 任务单 / 样品只读适配

> 状态：Excel/CSV 只读适配、报价对账、降级路径与 CRM 端点已完成；真实 CRM API
> 保持预留（不调用），需要客户接口账号与字段口径才能替换。

## 1. 目标与边界

P1-5 先解决“没有 CRM 接口也能对账”的问题：

- **只读**：项目、价格、资质、排期、任务单、样品只从 CRM 导出/接口读取，P0 不写回；
- **先 Excel/CSV**：客户导出文件即可接入，字段别名可配置；
- **接口预留**：`P0_CRM_MODE=api` 有明确错误“尚未接入”，不会静默改用旧数据；
- **报价对账**：报价行与 CRM 价格逐行匹配，输出 matched/missing/price_mismatch/price_available；
- **降级**：对账失败不阻断报价（默认），需要时可 `P0_CRM_REQUIRED=1` 强制 409。

不做：CRM 写回、审批流、自动改价、任务单/样品在 P0 内新建。

## 2. 数据模式

| `P0_CRM_MODE` | 行为 |
|---|---|
| `off`（默认） | 不加载 CRM；报价响应 `crm_reconciliation.enabled=false`，行为与旧版一致 |
| `excel` | 读取 `P0_CRM_PATH` 的 `.xlsx`；按 sheet 名/表头识别六类数据 |
| `csv` | 读取 `P0_CRM_PATH` 的 CSV，按价格清单解析 |
| `api` | 接口预留：有 base URL 时仍返回“尚未接入”，不发起请求 |

Excel 识别的 sheet/别名：

| 数据 | sheet 名别名 | 关键表头 |
|---|---|---|
| 项目 | 项目、项目清单、projects | 项目编号、项目名称、客户、状态、负责人、更新时间 |
| 价格 | 价格、价格表、报价、prices | 项目编号/项目名称、样品名称、检验项目、标准、科室、单位、数量、含税单价、折扣、有效期至 |
| 资质 | 资质、资质证书 | 资质名称、证书编号、适用范围、有效期至、状态 |
| 排期 | 排期、试验排期 | 项目编号、项目名称、计划开始、计划结束、状态、备注 |
| 任务单 | 任务单、任务 | 任务单号、项目编号、客户、样品名称、状态、委托日期 |
| 样品 | 样品 | 样品编号、项目编号、样品名称、型号、数量、状态、接收日期 |

没有标准 sheet 名时，第一张表按价格清单回退解析（兼容现有 CRM 批量导入模板）；
列名大小写/空格/全半角会归一化，`项目名称` 与 `检验项目` 的歧义通过“精确表头优先”处理。

## 3. 报价对账

`POST /api/v1/quotes/generate` 在存在报价行时追加：

```json
"crm_reconciliation": {
  "enabled": true, "mode": "excel", "reconciled": true,
  "total": 73, "matched": 73,
  "missing": [], "price_mismatch": [], "price_available": [],
  "crm_counts": {"projects":1,"prices":73,...}
}
```

匹配顺序：

1. 样品名称 + 检验项目 + 标准（标准只保留“大纲”类近似归一）；
2. 检验项目 + 标准（样品名不一致时兜底）；
3. 找不到 → `missing`；
4. 找到但报价单价与 CRM 相差 > 0.01 → `price_mismatch`；
5. 报价缺价但 CRM 有价 → `price_available`（可人工补价，当前不自动填入）；
6. 两边都没有价 → 视为“待补价”，不计入 mismatch。

`reconciled=true` 要求：全部行匹配、无价格差异、无“CRM 有价但报价未用”。零价行
不算差异，避免把“待补价”误判为对账失败。

`P0_CRM_REQUIRED=1` 时，未对账的报价返回 409，不写出 CRM/销售报价表；默认 0，
对账结果随响应返回，报价继续。

## 4. 端点与权限

| 端点 | 权限 |
|---|---|
| `GET /api/v1/crm/status` | `crm:read`（admin/reviewer/sales） |
| `GET /api/v1/crm/projects` | 同上 |
| `GET /api/v1/crm/prices?project=&q=&limit=` | 同上 |
| `GET /api/v1/crm/qualifications` | 同上 |
| `GET /api/v1/crm/schedules` | 同上 |
| `GET /api/v1/crm/task-orders/{order_no}` | 同上 |
| `GET /api/v1/crm/samples/{sample_no}` | 同上 |

viewer 不可读 CRM（403）；所有端点只读，无写回。CRM 数据可能含价格/客户信息，
只通过已登录会话暴露，日志不记录完整行内容。

## 5. 本地样例执行（2026-11-03）

- 使用本地 `CRM报价批量导入模板.xlsx`（73 行价格），`P0_CRM_MODE=excel`：
  - 识别 1 个项目、73 条价格；资质/排期/任务单/样品为 0（该样例只有价格表）；
  - 同一文件作为报价输入：73/73 行匹配，`reconciled=true`，无 missing/mismatch。
- 一个不相关项目 DVP（5 行）：0/5 匹配，`reconciled=false`，报价仍正常生成 CRM 草稿，
  仅返回 `missing` 列表；这是默认降级行为。
- `P0_CRM_MODE=api`：status 返回 `configured=false`，错误明确为“接口已预留但尚未接入”。
- `P0_CRM_REQUIRED=1`：同一 CRM 模板报价返回 200（73/73 已对账）；不相关 DVP 返回 409，
  detail 带 `crm_reconciliation`，不写出 CRM/销售表。

## 6. 测试

`tests/test_crm.py` 覆盖：

- 六类 sheet 的解析、字段查找、任务单/样品 lookup；
- 无标准 sheet 名时第一张表回退价格清单；
- CSV 解析；
- API 预留模式；
- 报价对账的匹配/缺失/价格差/可补价/零价；
- `P0_CRM_MODE=off` 的向后兼容；
- CRM 端点的 status/projects/prices/lookup/404。

全量测试：`100 passed, 2 skipped`（本地 PostgreSQL）。

## 7. 限制与待办

- **真实 CRM API 未接入**：没有接口账号、字段契约和网络策略；`api` 模式只是明确预留。
- 本地样例是“CRM 批量导入模板”（价格 73 行），不是完整 CRM 导出；资质、排期、任务单、
  样品表为空，需要客户提供真实导出或接口字段。
- 字段别名基于现有报价/CRM 模板推断，需客户确认最终字段名与编码（项目号、样品号、
  客户、任务单状态等）。
- 当前不自动用 CRM 价格填充报价缺价行；是否自动补价是业务决策（默认只对账）。
- 未实现 CRM 写回、任务单/样品在 P0 内新建、审批流。
- 真实样例与含客户名的对账结果只留工单，不入库。
