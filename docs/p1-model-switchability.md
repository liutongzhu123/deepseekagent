# T402 · 本地模型可切换性验证（2026-11-06）

> **2026-11-06 决策更新：`verified-deferred`**。本地 CPU 可跑通，但吞吐不足——约 **5.5 tok/s**、图像约 **62 s/张**；决定暂停本地模型，继续用 DeepSeek 公网 API。客户本地部署需要 GPU 时再评估。下文的“配置级可切换”结论保留，真实模型效果不再作为当前验收项。

> **结论先行**
> 1. **语义审核 → 本地 Ollama：配置级可切换**。不改代码，设置 `P0_SEMANTIC_BASE_URL`（含 `/v1`）、`P0_SEMANTIC_MODEL`、`P0_SEMANTIC_ENABLED=1`，并用任意非空 `DEEPSEEK_API_KEY` 占位即可；客户端请求已用 OpenAI 兼容 stub 验证。
> 2. **WeKnora VLM → Ollama `qwen2.5vl`：配置级可切换（需管理端配置）**。代码已有 `vlm_model_id` 与 Ollama 图片通道，不需要改代码；但需要 WeKnora 管理权限在模型管理里创建 `qwen2.5vl` 并绑定为 VLM，再用一份扫描件复跑。
> 3. 本次环境缺少真实 Ollama 端口/WeKnora 管理凭据/扫描件，**两处的真实模型效果未在本机跑通**；下文把已验证与未验证分开写，结论不替代真实样例验收。

## 1. 验证边界

| 项 | 本机可做 | 本机未做 |
|---|---|---|
| 语义审核客户端 → OpenAI 兼容端点 | 已做（本地 stub，请求/响应/错误路径） | 真实 Ollama 模型质量与真实报告 |
| WeKnora VLM 配置 | 代码与配置字段核对 | 管理端建模型、绑定、扫描件实测 |

本机 Ollama 运行在 WeKnora 容器网络（`weknora-ollama:11434`），宿主机 `127.0.0.1:11434` 不可达；WeKnora 管理 API 需要管理员凭据。因此没有把 stub 结果当成真实模型结果。

## 2. 语义审核切换到本地 Ollama

**代码入口**：`p0/app/semantic_audit.py`

- 配置读取：`P0_SEMANTIC_BASE_URL` / `P0_SEMANTIC_MODEL` / `P0_SEMANTIC_ENABLED` / `DEEPSEEK_API_KEY`（`load_config`）。
- 请求：`${base_url}/chat/completions`，OpenAI 兼容格式；Ollama 的兼容端点是 `/v1/chat/completions`，所以 base URL 必须写到 `/v1`。
- 未配置 key 时直接 `skipped`；Ollama 不需要 key，但仍需填一个占位值（例如 `ollama`），属于配置层，不需要改代码。

**验证方法（本机 stub）**：在 `127.0.0.1` 起一个 OpenAI 兼容 stub，按 Ollama 兼容端点的路径返回 `{"choices":[{"message":{"content":"{\"findings\":[]}"}}]}`，分别把 base URL 设为带 `/v1` 和不带 `/v1`，调用 `semantic_audit.audit_semantics`：

```bash
P0_SEMANTIC_ENABLED=1 \
DEEPSEEK_API_KEY=ollama-local-dummy \
P0_SEMANTIC_BASE_URL=http://127.0.0.1:<stub>/v1 \
P0_SEMANTIC_MODEL=qwen2.5vl:7b \
python - <<'PY'
from app import semantic_audit
print(semantic_audit.audit_semantics("样品名称：气味测试。结论：符合。", semantic_audit.load_config()))
PY
```

**实测结果（2026-11-06）**：

| base URL | 请求路径 | 结果 |
|---|---|---|
| `http://127.0.0.1:<stub>/v1` | `/v1/chat/completions` | `status=ok`，`model=qwen2.5vl:7b`，`findings=[]` |
| `http://127.0.0.1:<stub>` | `/chat/completions` | `status=error`，`transport: HTTP Error 404` |

**结论**：客户端侧是**配置级可切换**；上线前把 base URL 写到 `/v1`、填占位 key 即可。真实 Ollama 还需确认两点：模型是否支持 JSON 输出/是否忽略 `response_format`（请求里带了 `response_format: json_object`），以及输出能否稳定通过现有 `_parse_findings` 的 JSON 校验。若某版本 Ollama 对 `response_format` 报错，才需要小改代码去掉该字段；当前不能据此判定为“需要改造”。

## 3. WeKnora VLM 切换到 Ollama `qwen2.5vl`

**代码证据**（WeKnora v0.9.0 本地源码）：

- `internal/types/custom_agent.go`：`vlm_model_id`（VLM 模型）、`attachment_image_understanding`（图片/扫描页 VLM OCR 回退）。
- `internal/application/service/chat_pipeline/query_understand.go`：有图片时 `selectModel` 优先 `ChatModelSupportsVision`，否则回退 `VLMModelID`。
- `internal/models/api/images.go`：`ResolveImageURLForOllama`，图片走 Ollama 通道。
- 模型 provider 支持 Ollama；`qwen2.5vl` 是 Ollama 的视觉模型 tag（如 `qwen2.5vl:7b`）。

**配置步骤（管理端，无需改代码）**：

1. 确认 Ollama 已拉取 `qwen2.5vl:7b`（或客户现场可用版本）；
2. WeKnora 模型管理新增 Ollama 模型，base URL 指向 Ollama（容器内 `http://weknora-ollama:11434`），模型名 `qwen2.5vl:7b`，类型 VLM；
3. 在助手/自定义 Agent 配置里把 `vlm_model_id` 指到该模型；如要走扫描页 OCR 回退，同时打开图片理解/OCR 回退；
4. 用一份扫描件标准复跑解析与问答，记录页数、耗时、识别质量与失败页。

**未验证原因**：本机没有 WeKnora 管理凭据，容器内 Ollama 端口未暴露到宿主机，也没有可用的扫描件样例；因此没有真实跑通，不能宣称“已支持”。代码路径与配置字段齐全，判断为**配置级可切换**；若现场 Ollama 版本/显存不足或解析质量不达标，则属于模型选型/资源问题，不是接口改造。

## 4. 结论与后续动作

| 验证项 | 结论 | 需要改代码 | 待补验证 |
|---|---|---|---|
| 语义审核 → 本地 Ollama | **配置级可切换** | 否 | 真实 Ollama 模型 + 一份脱敏报告；确认 JSON 输出 |
| WeKnora VLM → `qwen2.5vl` | **配置级可切换（需管理端配置）** | 否 | 管理端建模型/绑定 + 一份扫描件 |

建议在客户本地部署验收时按上表补两项真实样例；在此之前，模型接入保持 `P0_SEMANTIC_BASE_URL` / `P0_SEMANTIC_MODEL` / `vlm_model_id` 可切换，不把公网模型写死。
