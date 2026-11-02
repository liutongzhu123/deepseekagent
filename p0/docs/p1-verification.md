# P1 前三项：改动、样例与自测

本文件记录本轮 P1 的可复核口径。真实客户材料（DVP、报价表、扫描件）只放在工单/服务器本地，
不提交仓库；下面给的是命令、合成样例和聚合结果。

## 1. 报价闭环

改动文件：

- `p0/app/quote.py`：`QuoteResult` 增加 `product_amount/service_amount/product_tax/service_tax/total_with_tax/final_price`；
  `fill_sales_quote()` 把金额写入 `C7/C8/C9/C10/C11/C12/C13/C14/C15`，新增「计算口径」sheet 保留模板原公式；
  `verify_sales_quote()` 重新打开生成文件，断言 9 个金额单元格 + 5 条算术关系，失败抛 `QuoteSelfCheckError`。
- `p0/app/main.py`：`POST /api/v1/quotes/generate` 生成后调用自检；失败返回 500，成功返回
  `sales_quote_verification`。
- `p0/scripts/run_quote_demo.py`：演示脚本生成后自检，报告写入 `quote_eval.json/quote_report.md`。
- `p0/tests/test_quote.py`、`p0/tests/test_api_quote.py`：单元格数值、算术自检、篡改报错、API 集成。

自测命令：

```bash
cd p0
pytest -q tests/test_quote.py tests/test_api_quote.py
python3 scripts/run_quote_demo.py \
  --dvp <DVP.xlsx> --crm-gold <真实CRM.xlsx> \
  --sales-template <销售报价表.xlsx> --out-dir <输出目录>
```

合成样例（2 行 DVP，单价 2000 + 4000×2，折扣 60%）：

| 单元格 | 字段 | 写入值 |
|---|---|---:|
| C9 | 产品金额（不含税） | 0 |
| C10 | 服务金额（不含税） | 6,000.00 |
| C13 | 税价合计 | 6,360.00 |
| C15 | 最终价格 | 6,360.00 |

真实前排座椅 DVP 样例：18 行；9 个金额单元格 + 5 条算术断言全部通过，
生成表内金额与计算结果一致（明细与金额留在工单/服务器本地，不入库）。

## 2. 知识问答向量检索

改动文件：

- `p0/app/embeddings.py`：Ollama `/api/embed` 客户端（bge-m3）、chunk embedding 文本、余弦相似度、磁盘向量缓存。
- `p0/app/standard_qa.py`：`answer_vector()` + `answer(mode=auto|vector|keyword)`；保留 `answer_keyword()` 基线；
  条款号/标准号守卫 + 相似度阈值（默认 0.60）。
- `p0/app/main.py`：`POST /api/v1/standards/ask` 接受 `mode/min_score/top_k`；显式 vector 不可用返回 503。
- `p0/scripts/run_kb_vector_demo.py`：5 个库内问题 + 2 个库外问题的关键词/向量对比。
- `p0/tests/test_qa_vector.py`：命中、条款选取、库外标准/条款拒答、阈值拒答、缓存、降级。

自测命令：

```bash
cd p0
pytest -q tests/test_qa_vector.py
python3 scripts/run_kb_vector_demo.py \
  --standards-dir data/standards --questions scripts/questions.example.json \
  --out-dir deliverables/qa-vector \
  --embedding-url http://127.0.0.1:11434 --model bge-m3
```

真实 2 份标准 / 129 片段结果：

| 指标 | 关键词 | 向量 |
|---|---:|---:|
| 5 个库内问题 + 2 个库外问题 | 6/7 | **7/7** |
| 同义改写问题（零件表面缺陷类） | 未命中 | **命中对应外观条款** |
| 库外标准 Q6 | 正确拒答 | 正确拒答 |
| 库外领域 Q7 | 正确拒答 | 正确拒答（top=0.5407 < 0.60） |

## 3. 扫描件 OCR

改动文件：

- `p0/app/ocr.py`：`pypdfium2` 渲染 + `rapidocr_onnxruntime` 本地 PP-OCR；行/页置信度。
- `p0/app/standard_parser.py`：文本层不足时走 OCR；支持「5 试验方法和性能要求」合并章节；
  输出 `confidence_detail/review_fields/review_required`，缺失/低置信度/含数值字段进入复核清单。
- `p0/app/main.py`：`POST /api/v1/standards/parse` 默认对扫描件启用 OCR（`enable_ocr`/`P0_OCR_ENABLED`）。
- `p0/scripts/run_ocr_demo.py`：OCR → 结构化 JSON/Excel + 复核清单。
- `p0/tests/test_ocr.py`：合并章节解析、字段置信度、低置信度/数值复核、缺失方法不编造。

自测命令：

```bash
cd p0
pytest -q tests/test_ocr.py
python3 scripts/run_ocr_demo.py --pdf <扫描件.pdf> --out-dir deliverables/ocr --dpi 220
```

真实 44 页扫描件结果（不提交原文）：

- OCR：`rapidocr_onnxruntime`，44 页，平均置信度 0.9667，220 DPI，约 97 s；
- 结构化：32 个检验项目，31 个有要求，32 个有方法，31 个条款可溯源；
- 复核：22 个项目、28 个字段级提示（表格/截断/低置信度/方法缺失）；
- 低置信度页：9、10、11、44（表格与签批页）。

输出字段示例（合成数据）：

```json
{
  "seq": 1,
  "name": "外观检查",
  "clause": "5.1.1",
  "requirement": "样品不应有裂纹、划痕等缺陷。",
  "method_clause": "5.1.1.3",
  "method": "目测检查并记录。",
  "confidence": 0.98,
  "confidence_detail": {
    "clause": 0.98, "name": 0.98, "requirement": 0.98,
    "method": 0.98, "method_clause": 0.98
  },
  "review_required": false,
  "review_fields": []
}
```

## 4. 全量自测

```bash
cd p0
pytest -q
```

结果：28 passed, 2 skipped（真实保密样例未入库）。
