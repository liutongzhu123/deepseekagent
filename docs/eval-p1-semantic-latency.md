# P1 语义审核单次耗时实测（本地 CPU，T406 补充）

> 日期：2026-11-07
> 目的：补 T406《模型与硬件选型建议》里“本地小模型 CPU 档语义审核单次耗时未计时”的缺口。
> 结论：本机可用的 **qwen2.5:3b**（CPU，无 GPU）单次语义审核 **P50 24.1 s / P95 26.0 s**（5 条合成短文本 × 2 次，带负载）。短文本已 >20 s，真实报告更长，本地 CPU 语义审核只能低频使用；公网 API 与 GPU 仍未实测。

## 1. 环境与口径

| 项 | 值 |
|---|---|
| 主机 | 8 vCPU（4 核 × 2），14 GB 内存，**无 GPU**；测试时 load average 约 3.2–6.0（与其他任务共用，测试期间上升） |
| 模型 | Ollama `qwen2.5:3b`（3.1B，Q4_K_M，CPU 推理，`size_vram=0`，context 4096） |
| 调用路径 | `p0/app/semantic_audit.py` → OpenAI 兼容 `/v1/chat/completions` → 本地 Ollama；`response_format=json_object`；timeout 240 s |
| 输入 | `p0/eval/semantic_cases.json` 的 5 条合成用例：S01/S02/S03/S04 各 1 条 + clean 1 条；每条 2 次，共 10 次 |
| 预热 | 先跑 1 次 clean-01，不计入结果（25.9 s） |
| 计时 | 请求发出到拿到并解析响应的墙钟时间；串行；模型已预热 |
| 分位 | 与 `p0/scripts/bench_p1_latency.py` 一致：排序后取最近秩 |

> 输入是合成的短文本（124–139 字），不是客户报告；没有客户数据参与。本次只测耗时，不评准确率。

## 2. 结果

| 用例 | 组 | 文本字符 | 第 1 次 | 第 2 次 | 状态 |
|---|---|---:|---:|---:|---|
| s01-01 | S01 | 133 | 25.2 s | 24.5 s | ok |
| s02-01 | S02 | 139 | 26.0 s | 25.9 s | ok |
| s03-01 | S03 | 133 | 24.5 s | 23.6 s | ok |
| s04-01 | S04 | 124 | 22.3 s | 22.5 s | ok |
| clean-01 | clean | 132 | 23.8 s | 24.1 s | ok |

| 样本 | P50 | P95 | 最小 | 最大 | 平均 |
|---:|---:|---:|---:|---:|---:|
| 10 | **24.1 s** | **26.0 s** | 22.3 s | 26.0 s | 24.2 s |

- 10/10 状态 `ok`，没有超时或 schema 错误。
- 10 次调用每条都返回 1 条 finding，包括 clean-01；**本次只测耗时，不做准确率结论**。这个现象说明 qwen2.5:3b 的输出质量不能直接用于验收，质量需另做评测。
- 预热后单次约 22–26 s；第一次冷启动的单次调用曾到 56.0 s（模型加载/首包），不计入上表。

## 3. 解释与限制

- **短文本下限**：输入只有 124–139 字；真实报告更长，语义审核耗时通常会更高，24 s 应视为本次短文本口径，不是报告级 SLA。
- **模型口径**：qwen2.5:3b 是本机可用的最小模型；T402 的 5.5 tok/s 是另一模型/口径，不能直接换算成同一单次耗时。
- **负载影响**：测试时主机 load average 约 3.2–6.0，绝对值受并发负载影响；空闲时会低一些，但仍是 20 s 量级。
- **只测本地 CPU**：本机没有 GPU，也没有公网 DeepSeek 的有效 API Key；A 档（公网 API）与 C 档（GPU）的语义审核单次耗时**仍未实测**。
- **不含质量结论**：本次不比较语义召回/误报，也不替代 44 条合成用例或 D3 正式报告验收。

## 4. 待补测（GPU / 公网 API）

以下三项是 T406 下一步仍未完成的数据；本轮只把可复现的方法写清，不填虚假数字。

### 4.1 公网 DeepSeek 语义审核单次耗时

需要有效的 `DEEPSEEK_API_KEY`；当前环境没有。命令与本地测量相同，只换 base URL/model：

```bash
cd p0
P0_SEMANTIC_ENABLED=1 DEEPSEEK_API_KEY=... \
P0_SEMANTIC_BASE_URL=https://api.deepseek.com \
P0_SEMANTIC_MODEL=deepseek-chat \
python - <<'PY'
import json, time
from app import semantic_audit
cases = {c['id']: c for c in json.load(open('eval/semantic_cases.json'))['cases']}
picked = [cases[k] for k in ('s01-01','s02-01','s03-01','s04-01')]
picked.append(next(c for c in cases.values() if c.get('group') == 'clean'))
cfg = semantic_audit.load_config()
for c in picked:
    for _ in range(2):
        t0 = time.perf_counter()
        res = semantic_audit.audit_semantics(c['text'], cfg)
        print(c['id'], round((time.perf_counter()-t0)*1000, 1), res.get('status'))
PY
```

### 4.2 GPU 上的语义审核

把 `P0_SEMANTIC_BASE_URL` 指向 GPU 上的 Ollama/vLLM 兼容端点，模型换成现场要用的模型，重复 4.1 的 5 条 × 2 次；记录 P50/P95 与模型名。没有 GPU 时无法得到可信数字。

### 4.3 GPU 上的 rerank

需要两个前置条件：① WeKnora 支持可配置候选池（当前 0.9.0 固定 50）；② reranker 跑在 GPU 上。满足后用 `p0/scripts/bench_weknora_retrieval.py` 对同一组 5 题 × 2 次复测，目标仍是 **15 候选 P95 ≤ 2 s**（每候选 ≤ 0.133 s），并补 5 题命中质量对比。

## 5. 复现（本地 CPU）

```bash
cd p0
P0_SEMANTIC_ENABLED=1 DEEPSEEK_API_KEY=ollama-local \
P0_SEMANTIC_BASE_URL=http://127.0.0.1:11434/v1 \
P0_SEMANTIC_MODEL=qwen2.5:3b P0_SEMANTIC_TIMEOUT=240 \
python - <<'PY'
import json, statistics, time
from app import semantic_audit
cases = {c['id']: c for c in json.load(open('eval/semantic_cases.json'))['cases']}
picked = [cases[k] for k in ('s01-01','s02-01','s03-01','s04-01')]
picked.append(next(c for c in cases.values() if c.get('group') == 'clean'))
cfg = semantic_audit.load_config()
semantic_audit.audit_semantics(picked[-1]['text'], cfg)  # warm-up
times = []
for _ in range(2):
    for c in picked:
        t0 = time.perf_counter()
        res = semantic_audit.audit_semantics(c['text'], cfg)
        times.append(round((time.perf_counter()-t0)*1000, 1))
        print(c['id'], times[-1], res.get('status'))
ordered = sorted(times)
print('n', len(ordered), 'p50', ordered[int(round(0.5*(len(ordered)-1)))],
      'p95', ordered[int(round(0.95*(len(ordered)-1)))],
      'mean', round(statistics.mean(ordered), 1))
PY
```

## 6. 对 T406 结论的影响

- T406 的“本地 CPU 语义审核只能低频使用、交互式会明显等待”得到实测支持：短文本单次已约 24 s。
- T406 的主结论不变：**试点期不需要买 GPU**；只有“语义审核必须本地化且要可接受交互速度”或“确定要上 rerank”才进入 GPU 采购，且先做 GPU 复测。
- 公网 API 与 GPU 的单次耗时仍是缺口；补齐前，T406 对应单元格继续写“未实测/未单独计时”。
