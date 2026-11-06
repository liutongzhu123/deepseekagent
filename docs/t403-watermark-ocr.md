# T403 · 带水印样例确认与 OCR 前后对比（2026-11-07）

> 样例：`/opt/dsh-agent/samples/watermark-sample.pdf`（33 页，约 21 MB，`Q/JLY J7111074B-2021`）。
> 目标：先确认水印形态，再比较「去水印前 vs 去水印后」OCR，供决定策略；本文只记录实验，不宣布最终方案，不改代码。

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

**建议**：A 为主（数字版 PDF 精确、可回退），B 只作扫描件兜底，并加「处理后行数/字符数对比 + 人工复核」校验；C 只做兜底。现有 `p0/app/watermark.py` 已覆盖标准号重复串和 CJK 间孤立字母，但**尚未覆盖 TO/OF/BY/AT 块**；是否加这条规则、颜色阈值取多少，请确认后我再改代码。

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
