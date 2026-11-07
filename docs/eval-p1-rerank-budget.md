# T405 · rerank 候选集预算评估（50 → 10/15）

> 日期：2026-11-07
> 任务：基于 T401 实测「直连 rerank P95 54.6 s」，实测把 rerank 候选集从 50 降到 10/15 能把耗时压到多少；只有压到 **2 s 以内**才进入接入问答的收益评估。
> 结论：**不能接**（当前 CPU 部署）。当前不把 rerank 接进问答链路，P0 默认保持 hybrid-search。

## 1. 结论

- 同一本地 CPU reranker 上，15 个候选实测 P50 **37.8 s** / P95 **40.1 s**，10 个候选 P50 **24.9 s** / P95 **25.5 s**（2026-11-07 10:20–10:45，主机负载 2.5–3.5），均比 2 s 门槛高 12–19 倍。
- 即使按 T401 空闲主机上 50 候选 52.1 s 的每候选成本（约 1.04 s/候选）折算，15 候选仍需约 15.6 s、10 候选约 10.4 s，仍 >2 s；**减少候选数量本身达不到 2 s**。
- 更关键：WeKnora 0.9.0 把 rerank 候选池硬编码为 **50**（`DefaultRetrievalTopK=50`，`max(top_k, 50)`），请求里的 `match_count` / `rerank.top_k` 不能把池子降到 10/15。本报告的 10/15 是**离线回放**（对同一 50 候选的前 N 个调用 reranker），不是可配置的端到端路径。
- 因此不进入第 2 步（T401 5 题命中质量对比）；要接必须先解决「候选池可配置」+「GPU/更小模型」，再重测。

## 2. 测量口径

| 项 | 值 |
|---|---|
| 主机 | 8 vCPU，无 GPU；load average 2.5–3.5（与其他 agent 共用） |
| rerank 服务 | `bge-reranker-v2-m3`，`http://127.0.0.1:7997/rerank`；即 WeKnora `builtin-local-rerank` 配置的同一 `http://rerank:7997`（`/health` 返回 `threads=2`） |
| 问题集 | T401 的 5 题：`p0/eval/p1_latency_questions.live.json` |
| 候选来源 | 每题 `hybrid-search`（`match_count=50`）返回的前 50 个 chunk（平均 402–417 字，最大 512 字），共 5×50 |
| 调用 | `POST /rerank`，body `{"query": "...", "documents": [...]}`；不传 `top_n`，让模型给全部候选打分 |
| 样本 | 50：每题 1 次（5 个样本）；15/10：每题 3 次（各 15 个样本）；串行、服务已预热 |
| 耗时口径 | 请求发出到 JSON 返回的墙钟时间；只含 rerank 打分，不含 WeKnora hybrid 召回、查询改写和外网 |
| T401 基线 | 直连 `knowledge-search`（50 候选）：P50 52.1 s / P95 54.6 s，2026-11-07 00:55 左右、空闲主机 |

> 本次复测时主机有并发负载，50 候选实测 P50 126.4 s / P95 134.8 s，明显高于 T401 空闲时的 54.6 s；因此下表的绝对值是**当前负载下**的实测，15/50、10/50 的比例与 T401 同口径对照使用。两者都是实际调用计时，不是估算。
>
> 另：WeKnora 在调用 reranker 前会做 markdown 清洗（`internal/reranking/passage.go`），本测量直接送原始 chunk，未做清洗，长度略大于实际；因此本报告的耗时是保守上界，不会把结论推向「能接」。

## 3. 实测结果

| 候选数 | 样本 | P50 | P95 | 最小 | 最大 | 每候选 P50 |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 5 | 126.4 s | 134.8 s | 123.9 s | 134.8 s | 2.53 s |
| 15 | 15 | 37.8 s | 40.1 s | 32.9 s | 40.8 s | 2.52 s |
| 10 | 15 | 24.9 s | 25.5 s | 23.0 s | 26.0 s | 2.49 s |

> 15/50、10/50 的 P50 比例分别为 0.30、0.20；按 T401 空闲基线 52.1 s 折算约为 15.6 s、10.3 s（只作交叉校验，不是本次实测值）。
>
> 原始候选文本含客户 KB 内容，不入库；上表只保留聚合耗时。

## 4. 2 s 门槛判定

- 15 候选 P50 37.8 s、P95 40.1 s；10 候选 P50 24.9 s、P95 25.5 s；均 >2 s。
- 单候选（1 个文档）本次实测约 2.5 s；按 T401 空闲折算约 1.04 s。1–2 个候选也许能压到 2 s 内，但候选太少，rerank 已失去排序意义；10/15 候选在任何口径下都 >2 s。
- 用 T401 空闲基线折算：50 候选 52.1 s → 约 1.04 s/候选；15 ≈15.6 s、10 ≈10.4 s（折算只作对照，不是本次实测值），仍 >2 s。
- 要达到 15 候选 ≤2 s，每候选需 ≤0.133 s；10 候选需 ≤0.2 s。当前每候选约 1.04 s（空闲）/ 2.5 s（本次负载），差 5–19 倍。
- 结论：**不能接**（当前 CPU 部署）。不进入「5 题命中质量对比」，不把 rerank 设为问答默认路径。

## 5. 为什么候选池降不下来（WeKnora 0.9.0）

源码依据（部署版本 v0.9.0）：

- `internal/types/retrieval_config.go`：`const DefaultRetrievalTopK = 50`
- `internal/application/service/knowledgebase_search_rerank.go`：
  `pool := chunks[:min(len(chunks), max(topK, types.DefaultRetrievalTopK))]`
- 请求里的 `rerank.top_k` 只决定 rerank 后的结果数；`match_count` 只影响召回深度；候选池被 `max(topK, 50)` 抬到至少 50。`RerankOptions` 没有 `candidate_count` 字段。
- 附带发现：T401c 的 `P0_WEKNORA_RERANK_TOP_K=15` / `P0_WEKNORA_RERANK_CANDIDATES=30` 只改请求字段；T401 实测 meta 仍是 `candidate_count=50`，说明这两个环境变量没有减少 reranker 的实际打分数量。

因此：

- 50→10/15 不能通过现有 API/配置完成，必须改 WeKnora（把 50 的 floor 做成配置，或新增 `rerank.candidates`）并重建/重发布；这是另一仓/另一部署的改动，不在本 PR。
- 本报告的 10/15 是对同一候选池前 N 个的 reranker 回放，证明「即使能降，模型成本也不够低」。

## 6. 要接的话，下一步最小条件

1. WeKnora 侧提供可配置候选池（10/15），部署后复跑本报告的端到端计时；
2. reranker 改用 GPU 或更小/量化模型，使 15 候选 P95 ≤2 s（每候选 ≤0.133 s）；
3. 满足 1+2 后，用 T401 5 题跑 `p0/scripts/compare_retrieval.py`（hybrid vs rerank）对比命中率/引用准确率/拒答率，再决定是否默认开启；
4. 在此之前 P0 默认 `P0_WEKNORA_RERANK=0`；需要试验时显式设 `P0_WEKNORA_RERANK=1`。

## 7. 复现

```bash
# 凭据只从环境变量读，不写入仓库：
# WEKNORA_BASE_URL / WEKNORA_ADMIN_EMAIL / WEKNORA_ADMIN_PASSWORD / WEKNORA_KB_ID
cd p0
python3 - <<'PY'
import json, time, urllib.request
# 1) hybrid-search 取每题前 50 个候选（JWT 登录见 scripts/bench_weknora_retrieval.py）
# 2) 对每个 N=50/15/10，POST http://127.0.0.1:7997/rerank:
#    {"query": q, "documents": [c["content"] for c in candidates[:N]]}
# 3) 计时 = 请求发出到 JSON 返回；每题重复 1/3 次，取 P50/P95
PY
```

复现时注意：`/rerank` 单进程、`threads=2`；多个客户端并发会互相排队，务必串行并在服务空闲后开始。

## 8. 本次变更

- `p0/app/weknora.py`：`P0_WEKNORA_RERANK` 默认 `1`→`0`（opt-in），docstring 同步；T404 的代码保留，开关可回退。
- `p0/tests/test_rerank_params.py`：默认走 hybrid；新增 `P0_WEKNORA_RERANK=1` 走 knowledge-search 的用例。
- `p0/README.md`：rerank 默认值与「WeKnora 候选池仍为 50」的说明同步。
- `docs/PLAN.md`、`docs/eval-p1-latency.md`：看板与 T401 结论同步。
