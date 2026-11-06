# P1 检索延迟评测（T401）

> 更新时间：2026-11-06。任务：以现有 5 份标准的问答为基线，改前/改后对比 P95 首字延迟，目标 **P95 < 8s**。
> 口径说明：P0 的 `/api/v1/standards/ask` 是**同步 JSON**接口（没有 token 流），首字（首个可见内容）随响应到达；下文把“从请求到答案 JSON 返回”的墙钟时间称为**首结果延迟**，用它近似首字延迟。

## 1. 改前 / 改后配置

| 开关 | 改前（baseline） | 改后（after） |
|---|---|---|
| 查询改写 | `P0_QUERY_REWRITE=0`（不调用改写模型） | `P0_QUERY_REWRITE=1`，`P0_QUERY_REWRITE_MODEL=deepseek-chat`（非 reasoning） |
| 改写缓存 | `P0_QUERY_REWRITE_CACHE=0` | `P0_QUERY_REWRITE_CACHE=1`，`P0_REDIS_URL=redis://…`，TTL 300s |
| rerank | `P0_WEKNORA_RERANK=1` + `P0_WEKNORA_RERANK_TOP_K=30`（改前 30） | `P0_WEKNORA_RERANK=1` + `P0_WEKNORA_RERANK_TOP_K=15` + `P0_WEKNORA_RERANK_CANDIDATES=30` |
| embedding 批大小 | `P0_EMBEDDING_BATCH=32` | `P0_EMBEDDING_BATCH=64` |

> 如果生产当前是 `P0_WEKNORA_RERANK=0`，再补一组“无 rerank”基线，避免把“开 rerank”的增量算到 T401c 头上。

## 2. 已实测：改写模型选择（2026-11-06）

方法：同一组 5 条中文问题（“这个条款要求多少？”等），每条重复 3 次，共 15 次；直接测 DeepSeek `/chat/completions` 的调用耗时（不含检索）。命令见 §4.1。

| 模型 | n | P50 | P95 | 平均 | 最大 |
|---|---|---|---|---|---|
| `deepseek-chat`（非 reasoning，改后） | 15 | 832.5 ms | 1163.1 ms | 862.3 ms | 1518.3 ms |
| `deepseek-reasoner`（reasoning，改前对照） | 15 | 1326.9 ms | 1666.5 ms | 1359.9 ms | 1706.6 ms |

**结论**：改写模型从 reasoning 换到 `deepseek-chat`，单次改写 P95 降低约 **503 ms**（-30%）。这是 T401a 的直接收益；缓存命中时（T401b）这次改写调用被整体省掉。

## 3. 5 份标准端到端对比：**待跑（本机无可用的 5 份标准 KB / WeKnora 凭据）**

本机没有 5 份标准的已发布 KB、WeKnora API key 和预发环境凭据，无法在本地重放“5 份标准”问答，因此不填虚假数字。需要在有 5 份标准的环境执行：

```bash
cd p0

# baseline：改前（rerank 30 / 改写关 / 缓存关）
P0_QUERY_REWRITE=0 P0_QUERY_REWRITE_CACHE=0 \
P0_WEKNORA_RERANK=1 P0_WEKNORA_RERANK_TOP_K=30 P0_WEKNORA_RERANK_CANDIDATES=30 \
python scripts/bench_p1_latency.py --from-store --questions eval/p1_latency_questions.json \
  --repeat 3 --label baseline --json /tmp/p1-latency-before.json

# after：改后（rerank 15 / 改写 deepseek-chat / 缓存 5 分钟）
P0_QUERY_REWRITE=1 P0_QUERY_REWRITE_MODEL=deepseek-chat \
P0_QUERY_REWRITE_CACHE=1 P0_REDIS_URL=redis://127.0.0.1:6379/0 \
P0_WEKNORA_RERANK=1 P0_WEKNORA_RERANK_TOP_K=15 P0_WEKNORA_RERANK_CANDIDATES=30 \
python scripts/bench_p1_latency.py --from-store --questions eval/p1_latency_questions.json \
  --repeat 3 --label after --json /tmp/p1-latency-after.json

python scripts/bench_p1_latency.py --compare /tmp/p1-latency-before.json /tmp/p1-latency-after.json \
  --markdown /tmp/p1-latency-compare.md
```

问题集格式（`eval/p1_latency_questions.json`）：`[{"id":"q01","question":"…"}]` 或 `{"cases":[…]}`；`--from-store` 会加载本地已发布的全部标准（5 份）。

**待填对比表（在有 5 份标准的环境跑完后替换）**：

| 指标 | 改前 | 改后 | 变化 | 目标 |
|---|---|---|---|---|
| P50 首结果延迟 | 待跑 | 待跑 | — | — |
| P95 首结果延迟 | 待跑 | 待跑 | — | **< 8000 ms** |
| 命中率 | 待跑 | 待跑 | — | 不下降 |

## 4. 复现命令

### 4.1 改写模型微基准

```bash
P0_QUERY_REWRITE=1 P0_QUERY_REWRITE_MODEL=deepseek-chat \
DEEPSEEK_API_KEY=… python - <<'PY'
import time
from app import query_rewrite
cfg = query_rewrite.config_from_env()
for model in ("deepseek-chat", "deepseek-reasoner"):
    times = []
    for q in ["这个条款要求多少？", "外观检查怎么做？", "5.1 的要求是什么？",
              "样品数量不够算不算不符合？", "报告编号为空怎么判定？"]:
        for _ in range(3):
            t0 = time.perf_counter(); query_rewrite._http_transport(q, cfg, model)
            times.append((time.perf_counter() - t0) * 1000)
    print(model, sorted(times)[int(len(times)*0.95)-1])
PY
```

### 4.2 缓存命中

```bash
P0_QUERY_REWRITE=1 P0_QUERY_REWRITE_CACHE=1 P0_REDIS_URL=redis://127.0.0.1:6379/0 \
python - <<'PY'
from app import query_rewrite, rewrite_cache
cache = rewrite_cache.from_env()
cfg = query_rewrite.config_from_env()
print(query_rewrite.rewrite("5.1 的要求是什么？", config=cfg, cache=cache).metadata())  # cached=False
print(query_rewrite.rewrite("5.1 的要求是什么？", config=cfg, cache=cache).metadata())  # cached=True
PY
```

## 5. 验收状态

| 项 | 状态 |
|---|---|
| T401a 非 reasoning 改写 + 开关 | ✅ 已合并（#72）；微基准 P95 -503 ms |
| T401b Redis 缓存（5 分钟） | ✅ 已合并（#73）；单测覆盖命中/过期/降级 |
| T401c rerank 30→15 + batch | ✅ 已合并（#74）；请求体单测 |
| 5 份标准 P95 < 8s | ⏳ 待跑：本机缺 5 份标准 KB / WeKnora 凭据；命令已备好 |
