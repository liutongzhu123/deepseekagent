# T403 · 带水印样例确认与 OCR 前后对比（2026-11-07）

> 样例：`/opt/dsh-agent/samples/watermark-sample.pdf`（33 页，约 21 MB，`Q/JLY J7111074B-2021`）。
> 目标：先确认水印形态，再比较「去水印前 vs 去水印后」OCR；2026-11-07 已确认并实现「文本层块删除为主、图像掩膜兜底 + 校验」。

## 1. 水印形态

**文本层（可机读）**：每页 4 组完全相同的 4 行块，33 页共 132 组：

```
TO:<收件人邮箱>
OF:<单位>
BY:<研究院>
AT:<日期>
```

- 位置固定、斜排、浅粉色，压在正文上；页眉 `Q/JLY J7111074B-2021` 每页只出现 1 次，是标准号，**不是**水印，不能删。
- 文本层是完整的 4 行块，不是零散碎片；因此数字版 PDF 可以按块精确删除，不需要先 OCR。
- OCR 侧水印和正文行经常被合并，例如：

```
TO:<...>EPB 制动钳总成技术条件
OF:<...>Technical Specification of EPB Caliper Assembly
BY：...
AT：...
```

只按 `TO/OF/BY/AT` 正则删串会留下碎片或吞掉被合并的正文，需要在文本层或图像层处理。

## 2. OCR 对比（RapidOCR，2× 渲染）

**去水印方法 A（图像颜色掩膜）**：水印像素为浅粉（约 `RGB(252,223,226)`，`R>180`、`R-G>8`、`R-B>8`），把满足条件的像素置白，再 OCR；正文黑/灰像素不受影响。

| 页 | 去水印前行数 | 其中水印行 | 去水印后行数 | 其中水印行 | 备注 |
|---|---:|---:|---:|---:|---|
| 1（封面） | 27 | 10 | 16 | 0 | 英文企业标准行恢复；`发布` 不再被水印切断 |
| 5 | 45 | 9 | 38 | 0 | 标题/英文标题恢复；OCR 漏检 1 行英文引用（像素未被删，视觉仍在） |
| 11 | 82 | 10 | 74 | 0 | `5.3.10`/`5.3.11` 标题恢复；OCR 漏检 2 行英文段落（像素未被删，视觉仍在） |

**第 5 页差异摘录**（`-` 去水印前，`+` 去水印后；只保留与结论有关的行）：

```
- TO:<...>@<domain>EPB 制动钳总成技术条件
- OF:<...>天Technical Specification of EPB Caliper Assembly
+ EPB制动钳总成技术条件
+ Technical Specification of EPB Caliper Assembly
...
- This standard specifies ... test methods,
+ This standard specifies ... test methods,
+ Inspection rules, markings, packaging, transportation and storage ... for disc brake.
...
- T0 : Ch IP-Code) - Electric and Electronic Equipment)
+ (IP-Code)-Electric and Electronic Equipment)
...
- Degrees of protection (IP-Code) — Protection of electrical equipment against foreign objects, water and access)
  （去水印后 OCR 未输出此行；原图该行仍在）
```

**结论**：

- 颜色掩膜能把每页 9–10 行水印降到 **0**，并恢复被水印合并的标题、英文标题和正文行。
- 但 OCR 在第 5 页漏检 1 行、第 11 页漏检 2 行；核对原图后确认这些行**没有被像素删除**，是 OCR 行检测在掩膜后漏检。因此 OCR 行 diff 不能单独证明「没有内容丢失」，需要校验/回退。
- 文本层按 4 行块删除是确定性的：132 组全部删除，共移除 7,590 个字符（全部是水印块），第 5 页仍保留 `Degrees of protection` 和全部正文。数字版 PDF 优先用这条。

## 3. 策略对比与建议

| 策略 | 做法 | 对这份样例 | 风险/限制 |
|---|---|---|---|
| **A 文本层块删除**（推荐主路径） | 识别同页重复 ≥4 次的 `TO/OF/BY/AT` 四行块并删除 | 132 组全删；正文不丢；可回退 | 需要新增规则；水印字段/顺序变化时要覆盖 |
| **B 图像颜色掩膜**（扫描件兜底） | OCR 前把浅粉像素置白 | 水印 0 残留；标题恢复；OCR 可能漏检 1–2 行 | 红色印章/高亮可能误删；需阈值、开关和校验 |
| C 文本后处理正则 | OCR/文本层后删 `TO/OF/BY/AT` | 对文本层有效 | OCR 合并行会留碎片或吞正文 |

**已确认的实现（2026-11-07）**：A 为主，B 作扫描件兜底并加校验；C 不单独启用。

- **文本层**：`p0/app/watermark.py` 新增四行 `TO/OF/BY/AT` 块识别；同一块同页重复 ≥ `P0_WATERMARK_BLOCK_MIN`（默认 4）才删除，单次/不同块保留。样例 132 组全删，正文与页眉标准号保留。`P0_WATERMARK_CLEAN=0` 可整体关闭。
- **扫描件兜底**：`p0/app/ocr.py` 在 OCR 前做浅粉掩膜（`P0_OCR_WATERMARK_MASK` 默认 1；`P0_OCR_WATERMARK_MIN_RATIO` 默认 0.002）。掩膜前、掩膜后各 OCR 一次，按「非水印内容行是否缺失」校验；缺失则回退原图，并在 `warnings` 写「第 N 页去水印后校验发现内容行缺失，已回退原图，请复核」。`P0_OCR_WATERMARK_MASK=0` 可关闭。
- **文本后处理 C**：不单独做 `TO/OF/BY/AT` 正则删串；只保留 CJK 间孤立字母规则作兜底。标准号重复串规则已在 2026-11-07 删除（见 3.2），标准号整串不参与清洗。
- **样例路径**：这份 PDF 有可机读文本层，`parse_pdf_standard` 走文本层，不触发 OCR；图像掩膜只用于文本层缺失的扫描件。

### 3.1 应用校验（dpi 220，2026-11-07）

- 第 1 页：原始 29 行 / 掩膜后 16 行；非水印内容行 17→16，无长行缺失，应用掩膜；内容行数 -1 进入 `watermark_mask_review_pages`，解析 warning 提示复核。
- 第 5 页：原始 45 行 / 掩膜后 38 行；非水印内容行 34→38，`missing_lines` 为空，应用掩膜。
- 第 11 页：原始 84 行 / 掩膜后 76 行；非水印内容行 72→76，`missing_lines` 为空，应用掩膜。
- 2× 手工实验里第 5/11 页曾漏检 1–2 行；应用路径是 220 dpi + 掩膜前后各 OCR 一次 + 模糊行匹配，缺失则回退原图并写 warning；行数/字符数下降也会写复核 warning。OCR 结果里的 `watermark_mask` 保留 `before/after` 行数、字符数、`missing_lines` 等证据。

### 3.2 标准号误伤修复（2026-11-07）

**现象**：同一份 33 页带水印标准，只改 `P0_WATERMARK_CLEAN`：

| 清理开关 | 条款数 | 含「满足中」的条款 |
|---|---:|---:|
| `=0`（关闭） | 44 | 0 |
| `=1`（旧实现） | 44 | **23** |
| `=1`（修复后） | 44 | **0** |

修复后，开启/关闭清理的 44 条 `requirement` 逐条一致；`5.3.1.1` 恢复为
`满足 Q/JLY J7110440E-2021中真空密封性规定的技术要求`。

**根因**：`watermark.py` 的 `_STANDARD_TOKEN` 规则把**任何**同页重复 ≥4 次的标准号整串删除。
但标准正文反复引用其他标准是正常写法（`满足 Q/JLY J7110440E-2021中…`），该标准号在
第 9–20 页的表格里出现 4–8 次，于是被当成水印删掉，只剩「满足中…」。

**修法**：直接删除 `_STANDARD_TOKEN` 整串规则（对应方案 1）。`TO/OF/BY/AT` 四行块删除已覆盖
这份样例的文本层水印；标准号不再按出现次数参与清洗。`P0_WATERMARK_TOKEN_MIN` 随之移除。

**回归**：`p0/tests/test_watermark.py` 新增：

- 同页重复 4 次的引用标准号保持不变；
- `clean_pages` 不删引用标准号；
- 四行水印块删除时不影响同页引用标准号；
- `test_watermark_sample_keeps_cited_standard_numbers` 用同一份样例跑清理开/关，断言 44 条
  条款逐条一致、`5.3.1.1` 保留标准号、无「满足中」。样例未挂载时跳过，可用
  `P0_WATERMARK_SAMPLE` 指定路径。

**受影响条款快照（23 条，仅条款号）**：

```
5.3.1.1  5.3.1.2  5.3.1.3  5.3.2   5.3.3.1  5.3.4
5.3.5    5.3.6    5.3.7    5.3.8   5.3.9    5.3.13.1
5.3.13.2 5.3.13.3 5.3.13.4 5.3.14.1 5.3.14.2 5.3.14.3
5.3.14.4 5.3.20   5.3.23   5.3.24  5.3.25
```

> 为避免客户标准正文进入仓库，这里只记条款号；23 条修复前后的完整文本快照留在工单本地，
> 不入库。修复前它们都含「满足中…」，修复后均恢复为「满足 Q/JLY J7110440E-2021中…」。

## 4. 复现

```bash
# 文本层：统计/删除 4 行水印块（只读样例，不写回 PDF）
python - <<'PY'
import re, pypdfium2 as pdfium
pdf = pdfium.PdfDocument("/opt/dsh-agent/samples/watermark-sample.pdf")
block = re.compile(r"TO:[^\r\n]*\r?\nOF:[^\r\n]*\r?\nBY[：:][^\r\n]*\r?\nAT:[^\r\n]*\r?\n?")
for i, page in enumerate(pdf):
    text = page.get_textpage().get_text_range()
    print(i + 1, len(block.findall(text)), len(text), len(block.sub("", text)))
PY

# 图像层：第 5 页颜色掩膜 + 前后 OCR
python - <<'PY'
import numpy as np, pypdfium2 as pdfium
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
pdf = pdfium.PdfDocument("/opt/dsh-agent/samples/watermark-sample.pdf")
im = pdf[4].render(scale=2).to_pil().convert("RGB")
a = np.array(im).astype(int)
mask = (a[..., 0] > 180) & ((a[..., 0] - a[..., 1]) > 8) & ((a[..., 0] - a[..., 2]) > 8)
b = a.copy(); b[mask] = 255
engine = RapidOCR()
for name, img in (("before", im), ("after", Image.fromarray(b.astype("uint8")))):
    out, _ = engine(np.array(img))
    lines = [t for _, t, _ in (out or [])]
    print(name, len(lines), sum(any(k in t for k in ("TO:", "OF:", "BY:", "BY：", "AT:")) for t in lines))
PY
```
