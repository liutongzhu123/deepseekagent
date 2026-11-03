# 中汽零部件 P0/P1 · 智能审核与报价/知识问答服务

P0 的四个可运行切片：

- **报告审核**——确定性规则 + 记录校验 API（R01–R11），带问题清单/证据/建议与 Markdown 审核报告。
- **标准解析**——数字版标准 PDF → 条款/项目/技术要求/试验方法结构化结果，支持人工复核、Excel 导出、发布知识库与带引用问答。
- **报价助手**——DVP/测试大纲 Excel → CRM 报价草稿 + 客户「销售报价表」，价格/数量/单位来自输入，不做模型生成价格。
- **知识问答**——WeKnora 唯一知识底座检索，带标准号/条款/页码引用；库外标准、库外条款或相关度不足时明确拒答。

P0 受控上线（0.5）：

- **登录与审计**——账号密码登录、8 小时签名会话、角色（admin/reviewer/sales/viewer）、登录与业务 API 审计；
- **HTTPS**——同域名 `/p0/` 反向代理到本机 8090；登录、四条链路与下载均走 HTTPS；
- **WeKnora 唯一知识底座**——标准发布写入 WeKnora 专用知识库，问答只从 WeKnora 检索，引用可回溯；
- **四条链路**——报告审核、标准解析、报价助手、知识问答均有可操作 UI 与 API。

P1 前三项增量（2026-11-02 已合并）：

- **报价闭环**——产品金额/服务金额/税价合计/最终价格写入「销售报价表-生成.xlsx」实际单元格；生成后逐项自检，表内数字与计算不一致直接报错。
- **向量检索**——本地 `bge-m3` 向量检索作为离线/调试路径；生产问答已切到 WeKnora 唯一知识底座（0.5），保留关键词基线做前后对比。
- **扫描件 OCR**——`rapidocr_onnxruntime` + `pypdfium2` 本地 OCR，解析「试验方法和性能要求」合并章节，输出字段级置信度与人工复核清单。

P1-S1 第一个增量（0.6.0，身份 / 权限 / 审计）：

- **RBAC**——admin / reviewer / sales / viewer 四角色，统一权限矩阵，所有业务 API 显式校验；
- **账号安全**——密码策略（≥10 位、至少两类字符）、连续 5 次失败锁定 15 分钟、管理员启停/改角色/重置密码、SSO（OIDC）配置预留位；
- **项目隔离**——标准与报价可打项目标签；受限账号只能访问授权项目的数据，列表/详情/下载/发布/问答都做行级校验；
- **全量审计**——请求级审计 + 登录/用户/复核/发布/报价/导出等语义事件；管理员可按用户/事件/状态/时间查询并导出 CSV，支持保留期清理；
- **管理台**——管理员在「权限与审计」页签管理用户、角色、项目授权和审计查询。

P1 管理端规则配置增量（0.7.0，管理员定义三条链路的规则与字段）：

- **报告审核规则**——逐条启用/停用 R01–R11、改规则名称/说明/严重级别，配置均值容差与注意事项应有条数；
- **标准解析规则字段**——配置「技术要求 / 试验方法」章节别名、字段显示名、必填项（缺失进入人工复核清单）与 Excel 导出列；
- **报价助手规则字段**——配置项目/单价/数量/单位/样品表头别名、单位映射、默认折扣、产品/服务税率、CRM 报价草稿列；
- **即时生效与留痕**——保存后对新的审核/解析/报价请求生效，配置版本递增并写 `admin_config_updated` 审计事件；
- **安全边界**——`config:read` / `config:manage` 仅 admin 可用，未配置时使用与旧版本一致的默认规则。

P1 模块页内规则配置展示增量（0.7.1，管理端配置页保留）：

- **报告审核页**——右侧审核规则卡展示配置版本 / 更新时间、均值容差、注意事项条数、每条规则的启用状态与严重级别；
- **标准解析页**——顶部「解析规则字段」折叠卡展示章节别名、字段显示名、必填与 Excel 导出状态；
- **报价助手页**——顶部「报价规则字段」折叠卡展示折扣、税率、表头别名、单位映射与 CRM 草稿列，计算口径卡片同步显示当前折扣 / 税率；
- **只读边界**——三个模块页只展示生效配置，不提供编辑入口；管理员可通过「管理端配置」链接回到原规则配置页签，`GET /api/v1/config/effective` 对所有可读规则的角色开放且不返回配置文件的内部路径。

P1 配置入口与精准跳转增量（0.7.2）：

- **明显入口**——标准解析 / 报价助手顶部规则卡右侧提供「查看配置」按钮，点击后在弹窗中查看当前生效配置的完整明细；
- **修改配置**——管理员在弹窗底部点「修改配置」，跳转管理端「权限、审计与规则 → 规则配置」；
- **精准落点**——跳转后自动滚动到「标准解析规则字段」或「报价助手规则字段」并短暂高亮；URL 记录 `view=config&section=...`，刷新后仍定位到对应分区；
- **权限边界**——非管理员可以查看配置，但「修改配置」按钮不显示。

P1 异步任务增量（0.7.3，P1-2）：

- **后台解析**——大文件/扫描件上传返回 `202 + task_id`，OCR/解析在 worker 线程执行，期间 `/health` 与其他 API 保持响应；
- **进度与阶段**——标准解析页与发布按钮显示排队/提取/OCR/解析/保存阶段、百分比与失败原因；
- **取消、重试与恢复**——任务可取消、失败可重试；进程重启后按租约把中断任务重新入队，任务不丢；
- **发布异步**——标准发布（WeKnora 索引）同样进入任务队列，前端轮询到已发布后自动刷新；
- **兼容**——`P0_TASKS_ENABLED=0`（默认）或小文本 PDF 仍走原同步路径，API 字段不变；
- 运行手册与限制见 [`docs/p1-async-tasks/README.md`](../docs/p1-async-tasks/README.md)。

P1 WeKnora 运营与评测增量（0.7.3，P1-3）：

- **发布幂等**——本地 `knowledge_id` 失效或缺失时，按确定性标题查找已有知识并更新；多个同名保留 completed/最新的一条；
- **元数据对齐**——发布后写 `custom_metadata`（P0 标准 ID、标准号、名称、修订、项目），供去重与引用核对；
- **运营脚本**——`scripts/weknora_ops.py` 只读体检摘要模型/解析状态/重复/失败，`--apply` 才清理；支持配置摘要模型；
- **评测基线**——`scripts/run_weknora_eval.py` + 22 条 demo 问答集，输出命中率/拒答率/引用准确率/延迟；
- 运行手册与限制见 [`docs/p1-weknora-ops/README.md`](../docs/p1-weknora-ops/README.md)。

P1 工作台 UAT 增量（0.7.3，P1-4）：

- **UAT 脚本**——`scripts/uat_smoke.py` 对运行中的服务跑登录/标准/复核/导出/发布/报告/报价/空态/错误/权限/审计；
- **真实样例**——5 份标准、12 份报告草案、3 个报价案例在本地 PostgreSQL + 异步任务环境执行，28/28 结构检查通过；
- **解析回退**——表格型材料标准“技术要求”没有条款标题时，按“试验方法”章节生成待复核候选，避免 0 项；
- **样例缺口**——人工金标准、20–30 份报告、部分定价规则仍需客户确认；
- 运行方式与结果见 [`docs/p1-uat/README.md`](../docs/p1-uat/README.md)。

P1 CRM 只读适配增量（0.7.3，P1-5）：

- **Excel/CSV 适配**——`P0_CRM_MODE=excel|csv` 读取本地 CRM 导出（项目/价格/资质/排期/任务单/样品），无标准 sheet 名时按价格清单回退；
- **报价对账**——报价生成追加 `crm_reconciliation`（matched/missing/price_mismatch/price_available）；默认降级不阻断，`P0_CRM_REQUIRED=1` 可强制 409；
- **只读端点**——`/api/v1/crm/status|projects|prices|qualifications|schedules|task-orders|samples`，`crm:read`（admin/reviewer/sales），viewer 403；
- **API 预留**——`P0_CRM_MODE=api` 明确提示“尚未接入”，不静默伪造数据；
- **本地样例**——CRM 批量导入模板 73 行价格与报价 73/73 对账；不相关 DVP 0/5 正常降级；运行手册见 [`docs/p1-crm-adapter/README.md`](../docs/p1-crm-adapter/README.md)。

P1 运维与安全收口增量（0.7.3，P1-6）：

- **监控指标**——`/metrics` 暴露请求/延迟/任务队列/DB/磁盘/备份年龄/WeKnora 配置；token 或 loopback 访问；
- **告警与巡检**——`deploy/monitoring/p0-alerts.yml` 覆盖服务、5xx、任务失败/积压、磁盘、备份、WeKnora、证书；`scripts/monitor_check.py` 可独立定时巡检；
- **结构化日志**——`P0_LOG_FORMAT=json` + `X-Request-ID` 贯通请求；不记录请求体/密码/文件内容；
- **备份恢复**——`scripts/restore_drill.py` 校验文件快照/数据层清单并可恢复文件；本地全量 DB 恢复演练通过；
- **依赖扫描**——`scripts/dependency_scan.py`；本轮升级 fastapi/starlette/python-multipart/pytest，`pip-audit` 漏洞从 28 清零；
- **等保差距清单**——见 [`docs/p1-ops-security/README.md`](../docs/p1-ops-security/README.md)（等级待客户确认）。

P1 报告审核台增量（0.8.0，审核历史 + 批量 + 项目隔离）：

- **审核历史**——每次审核留存结果、规则版本、审核人/项目/批次与原文；工作台显示最近记录，可回看完整问题清单；
- **批量审核**——一次多选 `.docx`，逐份审核并汇总成功/失败；单份失败不中断整批；批量可导出汇总 Excel；
- **导出与原文**——单份导出「审核汇总 + 问题清单」Excel，原文可下载；
- **项目隔离**——报告带项目标签，受限账号只能看授权项目内的记录与下载，跨项目 403；
- **存储**——文件模式写 `data/reports/<id>/`；`P0_STORE=postgres` 写 `report_audits` 表并把原文放对象存储；不迁移旧数据；
- 运行手册与限制见 [`docs/p1-report-workbench/README.md`](../docs/p1-report-workbench/README.md)。

## 运行

```bash
cd p0
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

或：

```bash
docker compose up -d --build
```

依赖包括 `pypdf`（PDF 文本层）、`pdfplumber`（合并表格回退）、`openpyxl`（DVP/报价表读写）、
`xlrd`（旧版 `.xls` 报价文件）、`Pillow`（图片读取）、`pypdfium2` + `rapidocr_onnxruntime`（扫描件/图片本地 OCR，免费、离线）。
若服务器 pip 镜像不可用，可把 wheel 解压到 `p0/vendor/`（已在 `.gitignore`），服务会自动优先加载 `pypdf`。

向量检索使用 Ollama 的 `bge-m3`，默认 `P0_EMBEDDING_URL=http://127.0.0.1:11434`；
WeKnora 服务器上 Ollama 只在容器网络内时，可设为容器地址（如 `http://<ollama-container-ip>:11434`）或先做端口转发。
`P0_EMBEDDING_URL=off` 可关闭向量检索、退回关键词模式。

## 访问与登录

生产入口：`https://<DOMAIN>/p0/`。未登录访问页面会跳转 `/login`，访问 `/api/*` 返回 401。
账号存放在 `data/auth/users.json`（PBKDF2-SHA256），会话是 HMAC 签名的 HttpOnly Cookie。
管理员可用接口或 `scripts/manage_users.py` 创建账号：

```bash
.venv/bin/python scripts/manage_users.py add <user> <admin|reviewer|sales|viewer>
.venv/bin/python scripts/manage_users.py reset <user>
```

角色与主要权限：

| 角色 | 报告审核 | 标准解析/复核/发布 | 报价 | 知识问答 | 用户/审计/规则 |
|---|---|---|---|---|---|
| admin | ✓ | ✓ | ✓ | ✓ | ✓ |
| reviewer | ✓ | ✓ | - | ✓ | - |
| sales | - | 只读 | ✓ | ✓ | - |
| viewer | - | 只读 | - | ✓ | - |

项目受限账号：`projects` 为空表示不限制项目；填写项目列表后，只能访问打了对应项目标签的标准/报价（以及自己创建但尚未打标签的记录）。受限账号新建标准/报价时必须填写有权限的项目。历史数据可由管理员调用 `PATCH /api/v1/standards/{id}/project` 补打项目标签。

认证与审计接口：

- `POST /api/v1/auth/login`（JSON `username/password`，成功后设置 Cookie；423 = 账号锁定）
- `POST /api/v1/auth/logout`
- `GET  /api/v1/auth/me`
- `GET  /api/v1/auth/providers`（本地账号 + OIDC 预留状态，公开）
- `GET/POST /api/v1/auth/users`（管理员）
- `PATCH /api/v1/auth/users/{username}`（管理员：角色/启停/姓名/部门/项目授权）
- `POST /api/v1/auth/users/{username}/reset-password`（管理员；不传密码时生成一次性临时密码）
- `POST /api/v1/auth/password`（本人改密）
- `GET  /api/v1/audit/events?user=&event=&status=&date_from=&date_to=&limit=&offset=`（管理员）
- `GET  /api/v1/audit/export.csv?...`（管理员）
- `GET  /api/v1/audit/recent`（管理员，最近审计事件）
- `GET  /api/v1/system/status`（登录后，版本/WeKnora 配置状态）
- `GET  /api/v1/admin/config`（管理员，当前规则配置 + 版本/来源路径）
- `GET  /api/v1/config/effective`（登录后，模块页只读生效配置；不含内部文件路径）
- `PUT  /api/v1/admin/config/{report|standard|quote}`（管理员，保存分区配置，先校验后原子写盘）
- `POST /api/v1/admin/config/{report|standard|quote}/reset`（管理员，恢复该分区默认值）

## 接口

### 报告审核

- `GET /health`
- `GET /api/v1/rules`
- `POST /api/v1/reports/audit`（multipart `file`，仅 .docx）→ JSON
- `POST /api/v1/reports/audit.md`（multipart `file`）→ Markdown 审核报告
- `POST /api/v1/records/validate`（JSON，`{"records":[...]}`）
- `GET /` 上传/演示页

规则：R01 页眉编号 / R02 均值 / R03 字段一致 / R04 日期顺序 / R05 设备溯源 /
R06 符合性判定 / R07 页码 / R08 注意事项 / R09 标记符号 / R10 签名印章 /
R11 模板识别。所有结论带 `rule_id/status/severity/evidence/suggestion`。
`GET /api/v1/rules` 返回每条规则的 `enabled/severity` 与启用数量；停用的规则在审核结果中为 `skip` 并说明原因。

### 标准解析

- `POST /api/v1/standards/parse`（multipart `file`，PDF；表单 `project` 项目标签，`enable_ocr` 控制 OCR）→ 解析结果 JSON
- `PATCH /api/v1/standards/{id}/project`（管理员，`{"project":"P-A"}`，给历史标准补项目标签）
- `GET /api/v1/standards` → 已解析标准列表
- `GET /api/v1/standards/{id}` → 完整结果（含 `review_history`）
- `PATCH /api/v1/standards/{id}` → 复核修改（按 `clause` 更新 `name/requirement/method`）
- `GET /api/v1/standards/{id}/export.xlsx` → 「实检项目」+「试验方法明细」；明细列由管理端字段配置决定（默认 7 列）
- `POST /api/v1/standards/{id}/publish` → 发布知识库（按条款切块）

解析策略：

1. 优先使用标准自带的检验项目表（如表 3）建立「技术要求条款 → 试验方法条款」映射；
2. 没有项目表时按标题名称相似度匹配；
3. 材料性能以跨页合并表格给出时，用 pdfplumber 读取表格几何、清洗 PDF 水印碎片、按方法条款/方法标题回填；
4. 文本层不足 500 字符时自动走 OCR；部分企业标准的「5 试验方法和性能要求」合并章节按项目=5.x.y、方法/要求=5.x.y.z 抽取；
5. 章节标题按中英文名称识别（含「技术条件／检验方法／测试方法」等近义词，管理端可增删别名）；扫描件只有中文标题时也参与识别；
6. 只有「试验方法」或只有「技术要求」一个章节的标准（如纯测定方法标准），按该章节的下级条款生成候选项目，缺失字段与项目名称进入人工复核，不再直接判 0 项；
7. 每条记录保留 `clause/method_clause/page/method_page`，可回溯原文；OCR 结果另带 `confidence_detail/review_fields/review_required`，名称相似度匹配与单章节候选项目也标 `review_required`/`review_fields`；管理端标记为必填的字段缺失时同样加入 `review_fields`。

### 报价助手

- `POST /api/v1/quotes/parse`（multipart `file`）→ 结构化明细、解析方式与汇总
- `POST /api/v1/quotes/generate`（multipart `file` + 可选 `template` + 表单 `project`）→ 生成 CRM 草稿并写入 `meta.json` 项目标签；提供 `销售报价表.xlsx` 模板且全部明细含单价时同时生成已填报价表并执行自检
- `GET /api/v1/quotes/{id}/crm.xlsx` → CRM 报价草稿
- `GET /api/v1/quotes/{id}/sales.xlsx` → 已填「销售报价表」（含「报价明细」「计算口径」sheet）

支持的报价输入（真实客户材料，20 MB 上限）：

| 格式 | 解析方式 | 说明 |
|---|---|---|
| `.xlsx` / `.xlsm` / `.xls` | Excel 表格 | DVP 表头 `试验项目 + 单价/数量/单位`（含章节内重复表头）、报价单表头 `样品名称/检验标准/检验项目/单价/数量/单位`、测试大纲 `试验项目/测试标准/数量` |
| `.pdf` | PDF 文本表格；无文本层时本地 OCR | 报价单/测试大纲/扫描件；扫描件最多 OCR 前 3 页 |
| `.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp` | 本地 OCR（RapidOCR） | 图片询价、手机截图；返回 OCR 置信度与复核提示 |

解析规则：单价、数量、单位只从输入读取；单位按字典映射（席位→席位/次、袋子→次等）；
折扣由销售政策参数传入（默认 60%），不由模型推断。图片/扫描件的行来自本地 OCR，
只做表格定位与文本抽取，不生成价格。管理端可配置表头/样品别名、单位映射、默认折扣、
产品/服务税率与 CRM 草稿列；这些参数对 Excel、PDF、图片三条解析路径生效。

缺少单价时（客户测试大纲、图片询价）：明细照常解析，`needs_price=true`，
CRM 草稿的「含税单价」留空并标注「待补单价」，**不生成**含金额的销售报价表；
API 返回 `sales_quote_skipped_reason`。这保证报价金额不会由模型或默认值臆造。

金额闭环：`C7/C8/C9/C10/C11/C12/C13/C14/C15` 写入实际数值（产品金额/服务金额/税价合计/最终价格为核心四项），
模板原公式保留在「计算口径」sheet；`verify_sales_quote()` 重新打开文件逐项断言 9 个金额单元格和 5 条算术关系，
不一致抛 `QuoteSelfCheckError`，API 返回 500。

### 知识问答（WeKnora 唯一知识底座）

- `POST /api/v1/standards/ask` → `{"question":"...","standard_id":"...","mode":"auto|weknora|local|keyword","top_k":6}`
  带引用问答；返回 `retrieval.mode/top_score/kb_id/knowledge_ids` 与 `citations[]`
- `POST /api/v1/kb/rebuild` → 重建本地条款级审计索引（不再作为生产问答来源）
- `GET /api/v1/kb/stats` → 本地索引统计

默认 `mode=auto`：调用 WeKnora 的混合检索（向量+关键词），用本地已发布 chunk 的元数据把命中内容映射回
标准号/条款/页码；`P0_WEKNORA_REQUIRED=1` 时 WeKnora 不可用直接 503，不会静默切回本地索引。
`mode=local|vector` 走本地 bge-m3（离线调试/前后对比），`mode=keyword` 是 P0 关键词基线。

守卫：条款号必须存在、标准号必须在库、相关度低于 `P0_WEKNORA_MIN_SCORE`（默认 0.35）时明确拒答。
标准发布时 `POST /api/v1/standards/{id}/publish` 会写入 WeKnora 的 P0 业务专用知识库；
重复发布更新同一条 knowledge；本地 `kb.json` 保留审计副本与 `weknora.knowledge_id`。
发布失败或本地 ID 丢失时，会按 `P0 | 标准号 | 标准名` 查找已有文档并更新，避免重复上传；
同时写入 `custom_metadata`（标准 ID/标准号/修订/项目）。运营与评测脚本见
[`docs/p1-weknora-ops/README.md`](../docs/p1-weknora-ops/README.md)。

问答只返回条款原文与标准号/条款/页码引用；标准或条款不在库时明确回答「未找到」，不编造。

## 标准解析 Schema（v0.1 + P1 OCR 字段）

每条记录：

`seq / name / group / clause / requirement / method_clause / method /
page / method_page / confidence / reviewed / references`

扫描件（OCR）额外带：

`confidence_detail`（clause/name/requirement/method/method_clause 字段级置信度）、
`review_required`、`review_fields[]`（字段、置信度、需复核原因）；
标准级 `meta.ocr` 记录引擎、页数、平均置信度、低置信度页和耗时。

- 项目与「技术要求/试验方法」条款、页码一一对应，可回溯原文；
- 引用 GB/T 未标年代号时，在 `references` 中标注已核实的现行版本，不改写原文；
- 所有自动结果默认 `reviewed=false`，复核修改写入 `review_history` 并递增 `revision`；
- 合并表格回退的项目 `confidence=0.7`，需人工复核。

## 演示脚本

脚本不包含客户数据，路径由参数传入；真实标准/报告/DVP 只放在本地 `data/` 或工单目录。

```bash
# 标准解析：2 个真实样例 + 准确率自评
python3 scripts/run_standard_demo.py --data-dir <标准目录> --out-dir <输出目录> \
  --epb-file <EPB.pdf> --pp-file <PP.pdf> --epb-gold <已复核EPB.xlsx>

# 报告审核：草稿批量 + 单份 Markdown 审核报告
python3 scripts/run_report_demo.py --reports-dir <报告目录> --out-dir <输出目录> [--sample <文件名>]

# 报价助手：DVP → CRM 草稿 + 销售报价表
python3 scripts/run_quote_demo.py --dvp <DVP.xlsx> --crm-gold <真实CRM.xlsx> \
  --sales-template <销售报价表.xlsx> --out-dir <输出目录>

# 报价助手：真实案例多格式自测（Excel / PDF / 图片）
python3 scripts/run_quote_multiformat_demo.py --files <DVP.xlsx> <报价单.pdf> <询价.png> \
  --out-dir <本地输出目录>   # 输出含客户项目名，勿提交仓库

# 知识问答：重建索引 + 5 个问答样例
python3 scripts/run_kb_demo.py --standards-dir <标准目录> --store-dir data/standards \
  --out-dir <输出目录> --epb-file <EPB.pdf> --pp-file <PP.pdf> \
  --questions <问题+评估口径.json> --reset

# P1 向量对比：5 个库内问题 + 2 个库外问题，关键词 vs bge-m3
python3 scripts/run_kb_vector_demo.py --standards-dir data/standards \
  --questions scripts/questions.example.json --out-dir deliverables/qa-vector \
  --embedding-url http://127.0.0.1:11434 --model bge-m3

# P1 扫描件 OCR：扫描件 → 结构化结果 + 置信度 + 复核清单
python3 scripts/run_ocr_demo.py --pdf <扫描件.pdf> --out-dir deliverables/ocr --dpi 220
```

## 数据

`P0_STORE=json`（默认）仍使用 `data/` 下的 JSON/文件；`P0_STORE=postgres` 后，
用户、审计、标准（含版本链/条款）、报价进入 PostgreSQL，原件/导出/报价表进入
MinIO 或本地对象目录，JSON 树作为迁移期镜像保留。切换、迁移、对账、回滚与备份
步骤见 [`docs/p1-data-layer/README.md`](../docs/p1-data-layer/README.md)。

默认保存在 `data/standards/`（已在 `.gitignore` 中，不入库）：

```
data/standards/<id>/source.pdf   上传原件
data/standards/<id>/result.json  解析/复核结果
data/standards/<id>/export.xlsx  Excel 导出
data/standards/<id>/kb.json      发布后的知识片段
data/standards/kb_index.json     重建后的合并索引
data/standards/kb_vectors.json   本地 bge-m3 向量缓存（仅调试）
data/auth/users.json             用户与密码哈希（0600）
data/auth/session_secret         会话签名密钥（0600）
data/audit/audit.jsonl           登录、业务 API 与语义事件审计（P0_AUDIT_RETENTION_DAYS 保留期）
data/quotes/<id>/                CRM 草稿、销售报价表与 meta.json（项目/创建人）
data/config/rules.json           管理员规则配置（0600，含报告/标准/报价三个分区）
backups/                         本地数据快照（0600，已在 .gitignore 中，不入库）
```

可用 `P0_STANDARD_DIR`、`P0_QUOTE_DIR`、`P0_SALES_QUOTE_TEMPLATE`、`MAX_STANDARD_MB` 调整目录/模板/上限；
数据层可用 `P0_STORE`、`P0_DATABASE_URL`、`P0_OBJECT_STORE`、`P0_OBJECT_DIR`、
`P0_MINIO_ENDPOINT`、`P0_MINIO_ACCESS_KEY`、`P0_MINIO_SECRET_KEY`、`P0_MINIO_BUCKET`、
`P0_MINIO_SECURE`、`P0_STORE_MIRROR` 调整；完整说明见 `docs/p1-data-layer/README.md`；
后台任务用 `P0_TASKS_ENABLED`、`P0_TASKS_CONCURRENCY`、`P0_TASKS_PARSE_ASYNC`、
`P0_TASKS_PUBLISH_ASYNC`、`P0_TASKS_ASYNC_MIN_MB`、`P0_TASKS_LEASE_SECONDS` 调整，
见 `docs/p1-async-tasks/README.md`；
CRM 只读适配用 `P0_CRM_MODE`、`P0_CRM_PATH`、`P0_CRM_REQUIRED`（`api` 模式另有
`P0_CRM_API_BASE_URL`/`P0_CRM_API_KEY` 预留），见 `docs/p1-crm-adapter/README.md`；
监控用 `P0_METRICS_TOKEN`、`P0_LOG_FORMAT=json`、`P0_ACCESS_LOG`，见
`docs/p1-ops-security/README.md`；
`P0_CONFIG_PATH` 指定规则配置文件（默认 `data/config/rules.json`；文件不存在时使用默认规则）；
`P0_AUDIT_RETENTION_DAYS`（默认 180）、`P0_SESSION_TTL`（默认 8 小时）控制审计保留与会话时长；
`P0_OIDC_ISSUER` / `P0_OIDC_CLIENT_ID` 仅作为 SSO 预留配置，当前登录仍走本地账号；
OCR 可用 `P0_OCR_ENABLED=0` 关闭，`P0_OCR_DPI`（默认 220）、`P0_OCR_MAX_PAGES` 控制速度；
本地向量检索（仅调试）可用 `P0_EMBEDDING_URL`、`P0_EMBEDDING_MODEL`、`P0_VECTOR_MIN_SCORE`、
`P0_VECTOR_TOP_K`、`P0_VECTOR_CACHE` 调整。
WeKnora 用 `P0_WEKNORA_BASE_URL`、`P0_WEKNORA_API_KEY`、`P0_WEKNORA_KB_ID`、`P0_WEKNORA_REQUIRED`、
`P0_WEKNORA_MIN_SCORE`、`P0_WEKNORA_TOP_K`、`P0_WEKNORA_RERANK` 调整。

## 测试

```bash
pytest -q
```

CI 在 `.github/workflows/p0-ci.yml`：push/PR 到 `main` 且改动 `p0/**` 时，用 Python 3.12 安装依赖并跑 pytest。

真实样例与保密标准放在 `data/` 或服务器 `/opt/cq-ai-p0/data`，不提交仓库。

## 备份与恢复（P1-0.5）

数据迁移前的文件数据安全网，详见 [`docs/p1-ci-backup/README.md`](../docs/p1-ci-backup/README.md)。

```bash
# 快照（默认 P0_DATA_DIR/P0_BACKUP_DIR）；归档含账号哈希与客户数据，权限 0600
python scripts/backup_data.py --data-dir data --backup-dir backups --keep 14

# 只校验，不写盘
python scripts/restore_data.py backups/p0-data-<时间戳>.tar.gz --verify-only

# 恢复到空目录（非空目录需 --force，旧目录会保留为 *.pre-restore-<时间戳>）
python scripts/restore_data.py backups/p0-data-<时间戳>.tar.gz --data-dir /tmp/p0-restore-check
```

每日定时器与异机保留步骤见文档；备份目录不要放在 Web 可访问目录，也不要提交仓库。

数据层（PostgreSQL + 对象存储）的快照与恢复：

```bash
P0_DATABASE_URL=... P0_MINIO_*... \
  python scripts/backup_store.py --backup-dir /var/backups/cq-ai-p0 --keep 14
python scripts/restore_store.py --backup-dir /var/backups/cq-ai-p0 --verify-only
python scripts/restore_store.py --backup-dir /var/backups/cq-ai-p0 --yes
```

## 已知限制（P1 后）

- 扫描件 OCR 只针对印刷体；表格数值未做单元格级结构化，涉及表格/截断/低置信度页的字段会进入复核清单；OCR 在请求内同步执行（44 页约 1.5–2 分钟），期间单进程服务不响应其他请求，批量/并发需队列（P2）；
- 「5 试验方法和性能要求」合并章节已支持；只有单一章节（无「技术要求」或「试验方法」）的扫描件会生成待复核候选项目，项目名称与缺失字段仍需人工确认；
- 单一章节候选项目按章节下级标题（无下级时按叶子条款）生成，可能与业务「实检项目」口径不一致，需在复核页改名并补全缺失字段；
- 复杂合并表格、公式、修约规则尚未全自动，需人工复核；合并表的数值字段有水印噪声；
- 自动项目名称按标题/项目表生成，业务口径差异（如「拖滞力矩/扭矩」）通过复核修改；
- 报价助手不做 CRM 写回、审批流、客户侧自动报价；折扣不自动推断；
- 向量检索依赖 Ollama `bge-m3`；阈值 0.60 是在当前 2 份标准/129 片段上校准的，扩库后需复测；条款号/标准号守卫不替代语义评测；
- 保密标准只做本地规则解析与本地 OCR，不调用外部大模型/公网 OCR。
- P1-1 数据层已提供 PostgreSQL + MinIO 代码与迁移/回滚/备份脚本，但共享服务器上的
  PostgreSQL/MinIO 安装与生产切换尚未执行（见 `docs/p1-data-layer/README.md` 的限制）；
  未切换前仍是 JSON/JSONL 文件；
- 项目隔离目前到「项目」粒度；部门/客户级授权要等 CRM 字段契约确认后接入，OIDC/SSO 只预留了配置与状态接口，未实现登录跳转；
- 管理员重置密码后返回一次性临时密码，需要人工安全传递；尚未强制首次登录改密；
- 项目隔离覆盖标准与报价；报告审核不落库，暂不做行级隔离；
- 管理端规则配置是可校验的开关/阈值/别名/字段定义，不是任意规则脚本或 DSL：不能新增 R12 或编写自定义判定逻辑；
- 配置是 `data/config/rules.json` 单文件（0600），保存时校验并原子替换，`revision` 递增；无审批流、无按时间点回滚，误改后可「恢复默认」或从备份快照恢复；
- 规则变更只影响保存后的新请求，已生成的审核结果、已解析标准、已有报价不重算。
