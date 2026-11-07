# 视觉类检查：空壳与启用条件（2026-11-07）

> 覆盖需求：**1.2-15 同一份报告中照片是否有完全一样的**、**1.4-35 识别照片中设备标签运行状态**
> 现状：接口、开关、测试都已就位，**默认关闭**；重复照片检测已实现，设备标签检测是预留桩。

---

## 一、为什么先做空壳

需求里有两条依赖照片，但当前测试集**一张照片都没有**：

| 数据 | 情况 |
|---|---|
| 12 份比亚迪草稿（`samples/byd/*.docx`） | **嵌入图片 0 张**（实际检查过 `word/media/`） |
| POC 原始材料目录 | 只有 8 张报价相关截图 |
| 客户资料总目录（673 张图） | 全是产品截图与营业执照 |

没有带照片的报告，就**没有测试数据能验证**这两条检查。与其空跑，不如先把接口和开关做好，
数据到位立刻能接。

---

## 二、已实现 vs 预留

| 能力 | 状态 | 说明 |
|---|---|---|
| 重复照片检测（V01） | ✅ **已实现** | 8×8 平均哈希，汉明距离 ≤2 判为重复；对重编码/缩放鲁棒 |
| 设备标签状态识别 | ⏸ **预留桩** | 无模型无样例，固定返回 `skipped: model_not_configured` |

### 重复照片检测的已知局限

- 能识别**重编码、缩放**后的重复（哈希不变）
- **不能**识别裁剪、旋转、镜像——那需要特征点匹配（后续可换 pHash 或 SIFT）
- docx 的 `word/media` 没有可靠的页码映射，所以结论报"第 N 张图片"，不是"第 N 页"

---

## 三、开关与配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `P0_VISUAL_CHECKS` | `0`（关闭） | 整个模块的总开关 |
| `P0_VISUAL_DUP_HAMMING` | `2` | 判"同一张照片"的最大汉明距离，越大越宽松 |
| `P0_VISUAL_DUP_MIN_BYTES` | `2048` | 小于该字节数的图片跳过（图标、边框不参与比较） |
| `P0_VISUAL_LABEL_ENDPOINT` | 空（关闭） | 设备标签模型的接入地址；设了才会有后续实现 |

**默认全关**，所以升级不会改变现有审核输出。

---

## 四、接口

```
POST /api/v1/reports/visual-check
  multipart: file=<.docx>
  权限：report:audit
```

关闭时返回：

```json
{"enabled": false, "status": "skipped", "reason": "module_disabled",
 "images": 2, "findings": []}
```

开启后（实测：同一张图放两次）：

```json
{"enabled": true, "status": "ok", "images": 2, "duplicates": 1,
 "findings": [{"code": "V01", "rule": "同一报告中照片完全重复", "severity": "fail",
               "message": "报告中第 1 张与第 2 张图片完全相同或高度相似（汉明距离 0）",
               "images": [1, 2], "hamming": 0}],
 "equipment_labels": {"status": "skipped", "reason": "model_not_configured",
                      "hint": "设置 P0_VISUAL_LABEL_ENDPOINT 后启用。启用前需要客户提供设备标签样例（待机 / 运行 / 检修各若干张），否则无法校准模型。"}}
```

---

## 五、启用设备标签检查需要什么

1. **客户样例**：设备标签照片，覆盖 **待机 / 运行 / 检修** 三种状态各 3–5 张
   - 最好包含不同角度、不同光照，以及"标签被遮挡"的负例
2. **状态定义**：客户确认哪些状态算"有问题"（待机？检修？停用？）
3. **模型接入**：一个能返回 `{state: waiting|running|repair, confidence}` 的端点
   - 可以是本地 VLM（客户 GPU 部署时一并解决），也可以是通用视觉模型
4. 配好 `P0_VISUAL_LABEL_ENDPOINT` 后，把 `equipment_label_states()` 里的桩替换成真实调用

---

## 六、测试

`p0/tests/test_visual_checks.py`（7 个用例，全部通过）：

- 默认关闭 → `module_disabled`
- 相同图片 → 报 V01
- 不同图片 → 不报
- 小于阈值的图片 → 忽略
- 无模型时 → `model_not_configured`
- 从 .docx 抽图（不依赖 python-docx，手搓最小 zip）
- 端到端：同图放两次 → `duplicates=1`、`summary.fail=1`

> 注：项目 venv **没有 python-docx**（项目自带 `docx_parser`），所以测试用手工构造的最小 .docx zip，
> 避免为测试引入新依赖。
