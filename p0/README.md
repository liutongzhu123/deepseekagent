# 中汽零部件 P0 · 智能审核服务（第一/第二增量）

P0 的两个可运行切片：

- **第一增量：报告审核**——确定性规则 + 记录校验 API（R01–R11）。
- **第二增量：标准解析**——数字版标准 PDF → 条款/项目/技术要求/试验方法结构化结果，
  支持人工复核、Excel 导出、发布知识库与带引用问答。

## 运行

```bash
cd p0
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

或：

```bash
docker compose up -d --build
```

`pypdf==6.21.4` 由 `requirements.txt` 安装；若服务器 pip 镜像不可用，可把 wheel 解压到 `p0/vendor/`（已在 `.gitignore`），服务会自动优先加载。

## 接口

### 报告审核（第一增量）

- `GET /health`
- `GET /api/v1/rules`
- `POST /api/v1/reports/audit`（multipart `file`，仅 .docx）
- `POST /api/v1/records/validate`（JSON，`{"records":[...]}`）
- `GET /` 上传/演示页

规则：R01 页眉编号 / R02 均值 / R03 字段一致 / R04 日期顺序 / R05 设备溯源 /
R06 符合性判定 / R07 页码 / R08 注意事项 / R09 标记符号 / R10 签名印章 /
R11 模板识别。所有结论带 `rule_id/status/severity/evidence/suggestion`。

### 标准解析（第二增量）

- `POST /api/v1/standards/parse`（multipart `file`，PDF）→ 解析结果 JSON
- `GET /api/v1/standards` → 已解析标准列表
- `GET /api/v1/standards/{id}` → 完整结果（含 `review_history`）
- `PATCH /api/v1/standards/{id}` → 复核修改（按 `clause` 更新 `name/requirement/method`）
- `GET /api/v1/standards/{id}/export.xlsx` → 3 列「实检项目」+ 7 列「试验方法明细」
- `POST /api/v1/standards/{id}/publish` → 发布知识库（按条款切块）
- `POST /api/v1/standards/ask` → `{"question":"...","standard_id":"..."}` 带引用问答

示例：

```bash
curl -F "file=@Q-JLY J7111074B-2021-EPB制动钳总成技术条件.pdf" \
  http://127.0.0.1:8090/api/v1/standards/parse
curl -X PATCH -H 'Content-Type: application/json' \
  -d '{"reviewer":"标准工程师","items":[{"clause":"5.3.6","name":"活塞启动力压力"}]}' \
  http://127.0.0.1:8090/api/v1/standards/<id>
curl -o result.xlsx http://127.0.0.1:8090/api/v1/standards/<id>/export.xlsx
```

## 标准解析 Schema（v0.1）

每条记录：

`seq / name / group / clause / requirement / method_clause / method /
page / method_page / confidence / reviewed / references`

- 项目与「技术要求/试验方法」条款、页码一一对应，可回溯原文；
- 优先使用标准自带的检验项目表（如 EPB 表 3）做条款映射；没有表时按标题名称匹配；
- 引用 GB/T 未标年代号时，在 `references` 中标注已核实的现行版本，不改写原文；
- 所有自动结果默认 `reviewed=false`，复核修改写入 `review_history` 并递增 `revision`。

## 数据

默认保存在 `data/standards/`（已在 `.gitignore` 中，不入库）：

```
data/standards/<id>/source.pdf   上传原件
data/standards/<id>/result.json  解析/复核结果
data/standards/<id>/export.xlsx  Excel 导出
data/standards/<id>/kb.json      发布后的知识片段
```

可用 `P0_STANDARD_DIR` 改目录，`MAX_STANDARD_MB` 改上传上限（默认 40 MB）。

## 测试

```bash
pytest -q
```

真实样例与保密标准放在 `data/` 或服务器 `/opt/cq-ai-p0/data`，不提交仓库。

## 已知限制（P0）

- 扫描件不做 OCR/VLM：文本层不足时返回 0 项并给出明确告警；
- 复杂合并表格、公式、修约规则尚未全自动，需人工复核；
- 自动项目名称按标题/项目表生成，业务口径差异（如「拖滞力矩/扭矩」）通过复核修改；
- 保密标准只做本地规则解析，不调用外部大模型/公网 OCR。
