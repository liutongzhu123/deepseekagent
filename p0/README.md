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
`pypdfium2` + `rapidocr_onnxruntime`（扫描件本地 OCR，免费、离线）。
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

| 角色 | 报告审核 | 标准解析/复核/发布 | 报价 | 知识问答 | 用户/审计 |
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

### 标准解析

- `POST /api/v1/standards/parse`（multipart `file`，PDF；表单 `project` 项目标签，`enable_ocr` 控制 OCR）→ 解析结果 JSON
- `PATCH /api/v1/standards/{id}/project`（管理员，`{"project":"P-A"}`，给历史标准补项目标签）
- `GET /api/v1/standards` → 已解析标准列表
- `GET /api/v1/standards/{id}` → 完整结果（含 `review_history`）
- `PATCH /api/v1/standards/{id}` → 复核修改（按 `clause` 更新 `name/requirement/method`）
- `GET /api/v1/standards/{id}/export.xlsx` → 3 列「实检项目」+ 7 列「试验方法明细」
- `POST /api/v1/standards/{id}/publish` → 发布知识库（按条款切块）

解析策略：

1. 优先使用标准自带的检验项目表（如表 3）建立「技术要求条款 → 试验方法条款」映射；
2. 没有项目表时按标题名称相似度匹配；
3. 材料性能以跨页合并表格给出时，用 pdfplumber 读取表格几何、清洗 PDF 水印碎片、按方法条款/方法标题回填；
4. 文本层不足 500 字符时自动走 OCR；部分企业标准的「5 试验方法和性能要求」合并章节按项目=5.x.y、方法/要求=5.x.y.z 抽取；
5. 章节标题按中英文名称识别（含「技术条件／检验方法／测试方法」等近义词）；扫描件只有中文标题时也参与识别；
6. 只有「试验方法」或只有「技术要求」一个章节的标准（如纯测定方法标准），按该章节的下级条款生成候选项目，缺失字段与项目名称进入人工复核，不再直接判 0 项；
7. 每条记录保留 `clause/method_clause/page/method_page`，可回溯原文；OCR 结果另带 `confidence_detail/review_fields/review_required`，名称相似度匹配与单章节候选项目也标 `review_required`/`review_fields`。

### 报价助手

- `POST /api/v1/quotes/parse`（multipart `file`，.xlsx DVP/测试大纲）→ 结构化明细与汇总
- `POST /api/v1/quotes/generate`（multipart `file` + 可选 `template` + 表单 `project`）→ 生成 CRM 草稿并写入 `meta.json` 项目标签；提供 `销售报价表.xlsx` 模板时同时生成已填报价表并执行自检
- `GET /api/v1/quotes/{id}/crm.xlsx` → CRM 报价草稿
- `GET /api/v1/quotes/{id}/sales.xlsx` → 已填「销售报价表」（含「报价明细」「计算口径」sheet）

解析规则：识别 `试验项目 + 单价/数量/单位` 表头；单价/数量/单位只从输入读取；
单位按字典映射（席位→席位/次、袋子→次等）；折扣由销售政策参数传入（默认 60%），不由模型推断。

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
```

可用 `P0_STANDARD_DIR`、`P0_QUOTE_DIR`、`P0_SALES_QUOTE_TEMPLATE`、`MAX_STANDARD_MB` 调整目录/模板/上限；
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

真实样例与保密标准放在 `data/` 或服务器 `/opt/cq-ai-p0/data`，不提交仓库。

## 已知限制（P1 后）

- 扫描件 OCR 只针对印刷体；表格数值未做单元格级结构化，涉及表格/截断/低置信度页的字段会进入复核清单；OCR 在请求内同步执行（44 页约 1.5–2 分钟），期间单进程服务不响应其他请求，批量/并发需队列（P2）；
- 「5 试验方法和性能要求」合并章节已支持；只有单一章节（无「技术要求」或「试验方法」）的扫描件会生成待复核候选项目，项目名称与缺失字段仍需人工确认；
- 单一章节候选项目按章节下级标题（无下级时按叶子条款）生成，可能与业务「实检项目」口径不一致，需在复核页改名并补全缺失字段；
- 复杂合并表格、公式、修约规则尚未全自动，需人工复核；合并表的数值字段有水印噪声；
- 自动项目名称按标题/项目表生成，业务口径差异（如「拖滞力矩/扭矩」）通过复核修改；
- 报价助手不做 CRM 写回、审批流、客户侧自动报价；折扣不自动推断；
- 向量检索依赖 Ollama `bge-m3`；阈值 0.60 是在当前 2 份标准/129 片段上校准的，扩库后需复测；条款号/标准号守卫不替代语义评测；
- 保密标准只做本地规则解析与本地 OCR，不调用外部大模型/公网 OCR。
- P1-S1 的用户库与审计仍是 JSON/JSONL 文件（尚未迁 PostgreSQL），审计查询会全量扫描文件，数据量上来后需换 DB/索引；
- 项目隔离目前到「项目」粒度；部门/客户级授权要等 CRM 字段契约确认后接入，OIDC/SSO 只预留了配置与状态接口，未实现登录跳转；
- 管理员重置密码后返回一次性临时密码，需要人工安全传递；尚未强制首次登录改密；
- 项目隔离覆盖标准与报价；报告审核不落库，暂不做行级隔离。
