# 中汽零部件 P0/P1 · 智能审核与报价/知识问答服务

P0 的四个可运行切片：

- **报告审核**——确定性规则 + 记录校验 API（R01–R11），带问题清单/证据/建议与 Markdown 审核报告。
- **标准解析**——数字版标准 PDF → 条款/项目/技术要求/试验方法结构化结果，支持人工复核、Excel 导出、发布知识库与带引用问答。
- **报价助手**——DVP/测试大纲 Excel → CRM 报价草稿 + 客户「销售报价表」，价格/数量/单位来自输入，不做模型生成价格。
- **知识问答**——本地条款级索引重建、5 个带引用问答样例；标准不在库时明确拒答。

P1 前三项增量（本轮）：

- **报价闭环**——产品金额/服务金额/税价合计/最终价格写入「销售报价表-生成.xlsx」实际单元格；生成后逐项自检，表内数字与计算不一致直接报错。
- **向量检索**——知识问答接入现有 WeKnora 的 Ollama `bge-m3`（免费本地），替换 n-gram 关键词打分；保留关键词基线做前后对比；库外标准/领域仍明确拒答。
- **扫描件 OCR**——`rapidocr_onnxruntime` + `pypdfium2` 本地 OCR，解析「试验方法和性能要求」合并章节，输出字段级置信度与人工复核清单。

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

- `POST /api/v1/standards/parse`（multipart `file`，PDF；查询参数 `enable_ocr=true`）→ 解析结果 JSON
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
5. 每条记录保留 `clause/method_clause/page/method_page`，可回溯原文；OCR 结果另带 `confidence_detail/review_fields/review_required`。

### 报价助手

- `POST /api/v1/quotes/parse`（multipart `file`，.xlsx DVP/测试大纲）→ 结构化明细与汇总
- `POST /api/v1/quotes/generate`（multipart `file` + 可选 `template`）→ 生成 CRM 草稿；提供 `销售报价表.xlsx` 模板时同时生成已填报价表并执行自检
- `GET /api/v1/quotes/{id}/crm.xlsx` → CRM 报价草稿
- `GET /api/v1/quotes/{id}/sales.xlsx` → 已填「销售报价表」（含「报价明细」「计算口径」sheet）

解析规则：识别 `试验项目 + 单价/数量/单位` 表头；单价/数量/单位只从输入读取；
单位按字典映射（席位→席位/次、袋子→次等）；折扣由销售政策参数传入（默认 60%），不由模型推断。

金额闭环：`C7/C8/C9/C10/C11/C12/C13/C14/C15` 写入实际数值（产品金额/服务金额/税价合计/最终价格为核心四项），
模板原公式保留在「计算口径」sheet；`verify_sales_quote()` 重新打开文件逐项断言 9 个金额单元格和 5 条算术关系，
不一致抛 `QuoteSelfCheckError`，API 返回 500。

### 知识问答

- `POST /api/v1/kb/rebuild` → 重建全部标准的条款级索引，写 `data/standards/kb_index.json`
- `GET /api/v1/kb/stats` → 索引统计
- `POST /api/v1/standards/ask` → `{"question":"...","standard_id":"...","mode":"auto|vector|keyword","top_k":6}`
  带引用问答；返回 `retrieval.mode/top_score/candidates`

默认 `mode=auto`：用 bge-m3 向量检索，embedding 服务不可用时退回关键词并标 `keyword-fallback`；
`mode=vector` 在服务不可用时返回 503，`mode=keyword` 用于前后对比。
向量检索的守卫：条款号必须存在、标准号必须在库、未命中条款且相似度低于阈值（默认 0.60）时明确拒答。
向量缓存写 `data/standards/kb_vectors.json`，按 chunk id + 文本哈希校验，重建索引后自动失效。

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
data/standards/kb_vectors.json   bge-m3 向量缓存（P1，按 chunk 哈希失效）
data/quotes/<id>/                CRM 草稿与销售报价表
```

可用 `P0_STANDARD_DIR`、`P0_QUOTE_DIR`、`P0_SALES_QUOTE_TEMPLATE`、`MAX_STANDARD_MB` 调整目录/模板/上限；
OCR 可用 `P0_OCR_ENABLED=0` 关闭，`P0_OCR_DPI`（默认 220）、`P0_OCR_MAX_PAGES` 控制速度；
向量检索可用 `P0_EMBEDDING_URL`、`P0_EMBEDDING_MODEL`、`P0_VECTOR_MIN_SCORE`、`P0_VECTOR_TOP_K`、
`P0_VECTOR_CACHE` 调整。

## 测试

```bash
pytest -q
```

真实样例与保密标准放在 `data/` 或服务器 `/opt/cq-ai-p0/data`，不提交仓库。

## 已知限制（P1 后）

- 扫描件 OCR 只针对印刷体；表格数值未做单元格级结构化，涉及表格/截断/低置信度页的字段会进入复核清单；OCR 在请求内同步执行（44 页约 1.5–2 分钟），期间单进程服务不响应其他请求，批量/并发需队列（P2）；
- 「5 试验方法和性能要求」合并章节已支持；其他扫描件结构（独立技术要求/试验方法章节且无文本层）仍需补规则；
- 复杂合并表格、公式、修约规则尚未全自动，需人工复核；合并表的数值字段有水印噪声；
- 自动项目名称按标题/项目表生成，业务口径差异（如「拖滞力矩/扭矩」）通过复核修改；
- 报价助手不做 CRM 写回、审批流、客户侧自动报价；折扣不自动推断；
- 向量检索依赖 Ollama `bge-m3`；阈值 0.60 是在当前 2 份标准/129 片段上校准的，扩库后需复测；条款号/标准号守卫不替代语义评测；
- 保密标准只做本地规则解析与本地 OCR，不调用外部大模型/公网 OCR。
