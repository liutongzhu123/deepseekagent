# 中汽零部件 P0 · 智能审核与报价/知识问答服务

P0 的四个可运行切片：

- **报告审核**——确定性规则 + 记录校验 API（R01–R11），带问题清单/证据/建议与 Markdown 审核报告。
- **标准解析**——数字版标准 PDF → 条款/项目/技术要求/试验方法结构化结果，支持人工复核、Excel 导出、发布知识库与带引用问答。
- **报价助手**——DVP/测试大纲 Excel → CRM 报价草稿 + 客户「销售报价表」，价格/数量/单位来自输入，不做模型生成价格。
- **知识问答**——本地条款级索引重建、5 个带引用问答样例；标准不在库时明确拒答。

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

依赖包括 `pypdf`（PDF 文本层）、`pdfplumber`（合并表格回退）、`openpyxl`（DVP/报价表读写）。
若服务器 pip 镜像不可用，可把 wheel 解压到 `p0/vendor/`（已在 `.gitignore`），服务会自动优先加载 `pypdf`。

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

- `POST /api/v1/standards/parse`（multipart `file`，PDF）→ 解析结果 JSON
- `GET /api/v1/standards` → 已解析标准列表
- `GET /api/v1/standards/{id}` → 完整结果（含 `review_history`）
- `PATCH /api/v1/standards/{id}` → 复核修改（按 `clause` 更新 `name/requirement/method`）
- `GET /api/v1/standards/{id}/export.xlsx` → 3 列「实检项目」+ 7 列「试验方法明细」
- `POST /api/v1/standards/{id}/publish` → 发布知识库（按条款切块）

解析策略：

1. 优先使用标准自带的检验项目表（如 EPB 表 3）建立「技术要求条款 → 试验方法条款」映射；
2. 没有项目表时按标题名称相似度匹配；
3. 材料性能以跨页合并表格给出时（如 PP 表 2–6），用 pdfplumber 读取表格几何、清洗 PDF 水印碎片、按方法条款/方法标题回填；
4. 每条记录保留 `clause/method_clause/page/method_page`，可回溯原文。

### 报价助手

- `POST /api/v1/quotes/parse`（multipart `file`，.xlsx DVP/测试大纲）→ 结构化明细与汇总
- `POST /api/v1/quotes/generate`（multipart `file` + 可选 `template`）→ 生成 CRM 草稿；提供 `销售报价表.xlsx` 模板时同时生成已填报价表
- `GET /api/v1/quotes/{id}/crm.xlsx` → CRM 报价草稿
- `GET /api/v1/quotes/{id}/sales.xlsx` → 已填「销售报价表」（含「报价明细」sheet）

解析规则：识别 `试验项目 + 单价/数量/单位` 表头；单价/数量/单位只从输入读取；
单位按字典映射（席位→席位/次、袋子→次等）；折扣由销售政策参数传入（默认 60%），不由模型推断。

### 知识问答

- `POST /api/v1/kb/rebuild` → 重建全部标准的条款级索引，写 `data/standards/kb_index.json`
- `GET /api/v1/kb/stats` → 索引统计
- `POST /api/v1/standards/ask` → `{"question":"...","standard_id":"..."}` 带引用问答

问答只返回条款原文与标准号/条款/页码引用；标准或条款不在库时明确回答「未找到」，不编造。

## 标准解析 Schema（v0.1）

每条记录：

`seq / name / group / clause / requirement / method_clause / method /
page / method_page / confidence / reviewed / references`

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
```

## 数据

默认保存在 `data/standards/`（已在 `.gitignore` 中，不入库）：

```
data/standards/<id>/source.pdf   上传原件
data/standards/<id>/result.json  解析/复核结果
data/standards/<id>/export.xlsx  Excel 导出
data/standards/<id>/kb.json      发布后的知识片段
data/standards/kb_index.json     重建后的合并索引
data/quotes/<id>/                CRM 草稿与销售报价表
```

可用 `P0_STANDARD_DIR`、`P0_QUOTE_DIR`、`P0_SALES_QUOTE_TEMPLATE`、`MAX_STANDARD_MB` 调整目录/模板/上限。

## 测试

```bash
pytest -q
```

真实样例与保密标准放在 `data/` 或服务器 `/opt/cq-ai-p0/data`，不提交仓库。

## 已知限制（P0）

- 扫描件不做 OCR/VLM：文本层不足时返回 0 项并给出明确告警；
- 复杂合并表格、公式、修约规则尚未全自动，需人工复核；PP 表 2–6 的数值字段有水印噪声；
- 自动项目名称按标题/项目表生成，业务口径差异（如「拖滞力矩/扭矩」）通过复核修改；
- 报价助手不做 CRM 写回、审批流、客户侧自动报价；折扣不自动推断；
- 知识问答是本地条款检索基线，不是 WeKnora/向量检索；语义泛化能力有限；
- 保密标准只做本地规则解析，不调用外部大模型/公网 OCR。
