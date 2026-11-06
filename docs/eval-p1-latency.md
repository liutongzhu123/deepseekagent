# P1 检索延迟评测（T401）

> 更新时间：2026-11-07（补现网纯检索基线）；任务：以现有 5 份标准的问答为基线，改前/改后对比 P95 首字延迟，目标 **P95 < 8s**。
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
| P50 首字/首字节延迟 | 待跑 | 待跑 | — | — |
| P95 首字/首字节延迟 | 待跑 | 待跑 | — | **< 8000 ms** |
| 命中率 | 待跑 | 待跑 | — | 不下降 |

### 3.1 现网基线（`--endpoint http`）

脚本支持直接打现网 P0 接口，输出 `first_token_ms`（首字节/首字）与 `latency_ms`（完整响应）的 P50/P95。凭据只从参数/环境变量读，不写进仓库：

```bash
cd p0
P0_BENCH_BASE_URL=https://zhongqi.yunanht.com/p0 \
P0_BENCH_USERNAME=... P0_BENCH_PASSWORD=... \
python scripts/bench_p1_latency.py --endpoint http \
  --questions eval/p1_latency_questions.json --repeat 3 \
  --label live-baseline --json /tmp/p1-latency-live.json
```

**本机状态（2026-11-07 更新）**：`/p0/health` 可访问（0.8.4，auth=on）；已用只读 admin 账号补跑现网纯检索基线（见 §6）。现网响应没有 `rerank` 字段，带 rerank 组仍缺 WeKnora 租户 API key 或新版部署。

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
| T401 基准脚本（指定查询集 + P50/P95/首字） | ✅ 仓库内 `p0/scripts/bench_p1_latency.py`，支持本地/现网 `--endpoint http`、`--insecure` 与 `--mode` |
| 直连 WeKnora 基准脚本 | ✅ `p0/scripts/bench_weknora_retrieval.py`：JWT 登录 + `knowledge-search`（rerank）/ `hybrid-search`（无 rerank） |
| 现网纯检索基线（20 次） | ✅ 2026-11-07：`mode=weknora` P50 271.4 ms / P95 299.9 ms；`mode=keyword` P50 31.1 ms / P95 34.2 ms，见 §6 |
| 直连 WeKnora hybrid vs rerank（10 次/组） | ✅ 2026-11-07：hybrid 首字 P50 255.7 ms；rerank 首字 P50 52,136.2 ms，见 §6.5 |
| P95 < 8s（P0 语义路径） | ✅ 299.9 ms |
| P95 < 8s（直连 rerank） | ❌ 54,594.8 ms，远高于 8 s；瓶颈是本地 rerank 模型对 50 个候选打分 |

## 6. 现网基线与 rerank 对比状态（2026-11-07）

### 6.1 环境与口径

- 现网 P0 `0.8.4`，`https://zhongqi.yunanht.com/p0`，自签证书（脚本 `--insecure`）；admin 只读登录。
- 问题集 `p0/eval/p1_latency_questions.live.json`（5 题，均引用已发布标准条款，不含正文）。
- 5 题 × 4 次 = 20 次/组；每组先跑 1 次预热，预热不计入。
- P0 的 `/api/v1/standards/ask` 是同步 JSON，`first_token_ms`（首字节）≈ `latency_ms`（完整响应），见 §1 口径。
- `config` 块是**脚本侧**环境（`P0_QUERY_REWRITE=0`、`P0_WEKNORA_RERANK=0`），不是现网进程环境；现网响应没有返回 `retrieval.rerank` / `query_rewrite` 字段。

### 6.2 结果

| 组 | 路径 | n | 命中 | P50 首字 | P95 首字 | P50 完整 | P95 完整 |
|---|---|---:|---:|---:|---:|---:|---:|
| A | 现网 `mode=weknora`（P0 语义检索路径，不经 rerank） | 20 | 100% | 271.4 ms | 299.9 ms | 271.8 ms | 300.6 ms |
| B | 现网 `mode=keyword`（确定性关键词基线） | 20 | 100% | 31.1 ms | 34.2 ms | 31.4 ms | 34.7 ms |

- 首字最小/最大：A 257.2–302.7 ms；B 28.8–34.7 ms。
- 单题均值差异不大（A 262.6–300.8 ms），5 题负载稳定；P95 主要反映 WeKnora 往返抖动。
- 现网 `weknora` 路径走 P0 的 `mode=semantic`，本身**不经 rerank**；它是 P0 路径基线，不是 WeKnora 的 hybrid-search。hybrid vs rerank 的直连对比见 §6.5。

### 6.3 为什么 P0 响应里没有 rerank

现网 P0 `0.8.4` 的 knowledge-chat 走 `mode=semantic`，本身**不过 rerank**；WeKnora `0.9.0` 已部署 `WeKnora-rerank` / `bge-reranker-v2-m3`，`builtin-local-rerank` 是默认 rerank 模型。因此：

- 不能根据 P0 的响应字段判断 WeKnora 是否启用 rerank；
- hybrid vs rerank 对比必须直连 WeKnora 的 `/api/v1/knowledge-search`，见 §6.5；
- 认证是 `POST /api/v1/auth/login` 换 JWT，不是租户 API key；之前公网 `/api/v1` 的 502 是 frontend nginx 缓存了旧容器 IP，重启后 `/api/v1/health` 返回 401（正常）。

### 6.4 复现

```bash
cd p0
P0_QUERY_REWRITE=0 P0_WEKNORA_RERANK=0 \
python scripts/bench_p1_latency.py --endpoint http --insecure \
  --base-url https://zhongqi.yunanht.com/p0 \
  --username "$P0_USERNAME" --password "$P0_PASSWORD" \
  --questions eval/p1_latency_questions.live.json --repeat 4 \
  --mode weknora --label live-weknora --json /tmp/p1-latency-live-weknora.json
# 把 --mode 换成 keyword 跑 B 组；--repeat 4 × 5 题 = 20 次
```

### 6.5 直连 WeKnora：hybrid-search vs knowledge-search（rerank）

> 2026-11-07 补测。P0 的 `mode=weknora` 是 P0 语义检索路径；WeKnora 0.9.0 的 rerank 要直连 `/api/v1/knowledge-search`。

- 认证：`POST /api/v1/auth/login` `{"email": "...", "password": "..."}` → `.token`（JWT）；登录 200。不是租户 API key。
- 知识库：`0680bc21-1696-454d-b2c6-eec316a8874b`；rerank 模型 `builtin-local-rerank`（`bge-reranker-v2-m3`）。
- 端点与请求：
  - hybrid：`POST /api/v1/knowledge-bases/{kb}/hybrid-search`，`{"query_text": "...", "match_count": 6}`
  - rerank：`POST /api/v1/knowledge-search`，`{"query": "...", "knowledge_base_ids": [kb], "match_count": 30, "rerank": {"enabled": true, "top_k": 15}}`
- 问题集 `p0/eval/p1_latency_questions.live.json`（5 题，不含正文）；5 题 × 2 次 = **10 次/组**；脚本 `p0/scripts/bench_weknora_retrieval.py`。

| 组 | n | 命中 | P50 首字 | P95 首字 | P50 完整 | P95 完整 |
|---|---:|---:|---:|---:|---:|---:|
| hybrid-search（无 rerank） | 10 | 100% | 255.7 ms | 469.5 ms | 2,367.0 ms | 3,434.4 ms |
| knowledge-search（rerank） | 10 | 100% | 52,136.2 ms | 54,594.8 ms | 53,645.7 ms | 55,151.3 ms |

- 首字范围：hybrid 239.2–469.5 ms；rerank 50,674.1–54,594.8 ms。
- rerank meta：`model=builtin-local-rerank`、`candidate_count=50`、`result_count=11`、`top_score=0.999`。
- **结论**：rerank 首字 P50 **52.1 s** / P95 **54.6 s**，比 hybrid 的 255.7 ms 慢约 **204 倍**；远高于 T401 的 **P95 < 8 s** 目标。瓶颈是本地 rerank 模型对 50 个候选打分，不是检索/网络。
- **风险**：P0 的 `P0_WEKNORA_TIMEOUT` 默认 60 s，P95 54.6 s 只剩约 5.4 s 余量；在线路径启用 rerank 有 502 风险。
- **建议**：质量收益未量化前不要把 rerank 作为默认在线路径；要试先把候选池降到 10–15、换 GPU/量化模型或异步预计算，再复测 P95。

复现：

```bash
cd p0
WEKNORA_BASE_URL=https://zhongqi.yunanht.com \
WEKNORA_ADMIN_EMAIL=... WEKNORA_ADMIN_PASSWORD=... \
WEKNORA_KB_ID=... \
python scripts/bench_weknora_retrieval.py --insecure \
  --questions eval/p1_latency_questions.live.json --repeat 2 \
  --json /tmp/weknora-retrieval-live.json
```
