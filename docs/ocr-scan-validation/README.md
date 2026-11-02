# 扫描件 OCR 实测（2026-11-02）

## 环境

- P0 服务器：Ubuntu 24.04.5 LTS，4 vCPU，15.6 GB 内存（实测时约 12 GB 可用），
  Python 3.12.3（`/opt/cq-ai-p0/.venv`）。
- OCR 依赖：`rapidocr_onnxruntime 1.4.4`（PP-OCR 模型随包内置）、
  `onnxruntime 1.31.1`、`opencv-python 5.0.0.93`、`pypdfium2 5.14.0`；
  系统库 `libgl1`、`libglib2.0-0`（`opencv-python` 需要）。
- 渲染参数：pypdfium2，220 DPI（`P0_OCR_DPI` 默认值），逐页检测 + 识别，不调用云 OCR。
- 服务：`cq-p0-api`（uvicorn 单进程，`127.0.0.1:8090`）。重启后 `/health` 返回
  `version=0.5.0`、`auth=enabled`、`weknora=configured`。

## 样本与方法

- 样本：44 页保密扫描件，14.4 MB，无可用文本层；原件只存放在服务器
  `data/ocr-validation/`，不进入仓库。
- 接口：带登录会话调用 `POST /api/v1/standards/parse`（loopback 直连，避免公网
  上传带宽影响耗时）；另用 `app.ocr.ocr_pdf` 直接计时并取逐页置信度。
- 复核规则：字段置信度 < 0.90、含数值的字段所在页 < 0.95、字段缺失、章节含表格或
  文本截断、结构回退，均标记到 `review_fields`。

## 结果

### 耗时

| 项 | 数值 |
|---|---|
| 44 页 OCR | 约 99 s（接口端到端 98.6 s；引擎直测 99.3 s），折合 **2.25–2.26 s/页** |
| 结构解析（`parse_ms`） | 18 ms |
| 单页示例（封面，稀疏版式） | 1.5 s |
| 4 页密集表格片段 | 11.0 s（2.7 s/页） |

耗时随版式密度变化；44 页整份扫描约 1 分 40 秒。

### 置信度

- 页面级：平均 **0.9662**，最低 **0.816**（第 44 页）；低于 0.90 的页为
  9、10、11、44（0.832 / 0.858 / 0.835 / 0.816），第 10 百分位 0.928；
  共识别 2200 行。
- 字段级（32 个项目）：`clause`/`name`/`requirement`/`method`/`method_clause`
  均值 **0.979**，最低 **0.928**；1 个项目缺少 `requirement`，该字段置信度为 0。
- 项目级（取字段最小值）：中位 0.980，均值 0.913，最低 0（因上述缺失字段）。

### 人工复核项

解析出 32 个项目，其中 **22 个（69%）需要人工复核**，共 30 个复核字段：

| 原因 | 数量 |
|---|---|
| 章节含表格或文本被截断，需对照原 PDF | 19 |
| 字段含数值且所在页 OCR 置信度低于 0.95 | 4 |
| 未找到试验要求小节，回退正文 | 4 |
| OCR/结构解析未提取到该字段 | 2 |
| 未找到试验方法小节，回退试验条件 | 1 |

按字段：`method` 22、`requirement` 7、`method_clause` 1。

低置信度直接触发的复核只有 4 项；绝大多数复核来自**表格结构/文本截断与结构回退**。
即 OCR 文字识别本身较稳，表格版式的结构化仍需人工核对。

### 并发限制

- 服务端是 **uvicorn 单 worker**，且 OCR 在 `async` 处理函数内同步执行，会占满事件
  循环。44 页扫描期间，其他请求全部排队：实测对 `/health` 连续 8 s 超时（扫描约
  98 s 内无法响应），解析结束后立即恢复到 0.7 ms。
- 两个并发 4 页解析请求：10.0 s 与 19.5 s——第二个请求等第一个完成，**服务端实际
  并发度为 1**。
- 直接对引擎做并发压测（4 页密集表格片段，4 vCPU）：

  | 并发任务 | 总耗时 | 单任务耗时 | 相对吞吐 |
  |---|---|---|---|
  | 1 | 10.97 s | 10.95 s | 1.00 |
  | 2 | 19.85 s | 19.73 / 19.83 s | 1.11 |
  | 4 | 45.58 s | 43.8–45.6 s | 0.96 |

  单个 OCR 任务占 2.0–2.4 核；2 并发只提升约 11%，4 并发因争抢线程反而略低于单任务。

- 结论：**单实例 OCR 并发上限为 1**（最多 2 路并接受延迟上升）。44 页扫描约 100 s，
  期间该实例的其他 API 请求排队；批量/多人使用需要任务队列 + 429，或独立 OCR
  worker/多实例。受 4 vCPU 限制，多实例的实际有效并发也只有 2 路左右。

### 风险与限制

- 只实测 1 份扫描件（44 页，印刷体表格），未覆盖倾斜、低分辨率、手写、印章遮挡等
  质量差异。
- OCR 在请求线程内同步执行，扫描时 `/health` 也会阻塞，systemd `Restart=always`
  无法区分“正在扫描”与“假死”；建议后续增加任务队列/异步任务或独立 OCR 服务。
- 本次只安装依赖、重启服务并记录实测，未改业务代码；样本为保密件，未提交仓库。

## 复现

```bash
# 服务器
cd /opt/cq-ai-p0
.venv/bin/python -c "import cv2, onnxruntime; from rapidocr_onnxruntime import RapidOCR; RapidOCR(); print('ocr ok')"

# 登录后（密码从标准输入读取）
BASE=https://<DOMAIN>/p0
read -rs PW
curl -s -c /tmp/p0.cookies -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" "$BASE/api/v1/auth/login"
curl -s -b /tmp/p0.cookies -F file=@/path/scan.pdf "$BASE/api/v1/standards/parse"
```
