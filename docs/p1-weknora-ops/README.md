# P1-3 WeKnora 运营与评测

> 状态：发布幂等/元数据对齐、运营脚本、22 条问答评测集与本地基线已完成；
> 服务器上的知识库清理、摘要模型配置和生产评测需要 WeKnora 管理权限，尚未执行（见 §7）。

## 1. 目标与范围

WeKnora 是 P0 的唯一知识底座。本轮处理计划中的四件事：

1. **发布幂等**：同一 `standard_id + revision` 重复发布只更新同一条 knowledge，不新增重复文档；
2. **知识库运营**：可重复地检查重复/解析失败/卡住记录，按显式 `--apply` 清理，核对摘要模型；
3. **引用一致性**：发布正文的 `〔标准号 | 条款 | 页码 | 类型 | 项目〕` 头、`custom_metadata` 和 P0 本地记录互相对齐；
4. **评测基线**：20–30 条问答（库内命中、同义改写、库外拒答），记录命中率/拒答率/引用准确率/延迟。

不做：MCP、多轮会话、租户计费、WeKnora 权限体系重写。

## 2. 发布幂等与元数据

`app/weknora.py` 的 `publish_standard` 现在按以下顺序处理：

1. 如果本地记录已有 `weknora.knowledge_id`，先 `PUT /api/v1/knowledge/manual/{id}`；
2. 如果 PUT 404 或本地没有 ID，先按**确定性标题**（`P0 | <标准号> | <标准名>`）或
   `custom_metadata.p0_standard_id` 分页查找已有文档（改名/ID 丢失也能复用）；
3. 找到则复用/更新，多个同名时优先 `parse_status=completed`、更新时间最新的那条；找不到才 `POST` 新建；
4. 发布后写 `custom_metadata`：

   ```json
   {"p0_standard_id":"std_...","p0_standard_no":"Q/...","p0_name":"...",
    "p0_revision":3,"p0_project":"..."}
   ```

5. 元数据写入失败不回滚发布，但在返回值和 `weknora.metadata_error` 中保留告警，运营脚本会显示。

返回字段新增 `deduped`、`duplicates`、`metadata_updated`、`metadata_error`；原有字段不变。

> 知识库标题只含标准号/名称，不含版本；重复发布更新同一条知识。来源标准号为空时回退到 P0 标准 ID。

## 3. 运营脚本

脚本：`scripts/weknora_ops.py`。默认只读；`--apply` 才删除/修改。

```bash
# 只读体检：KB 配置、摘要模型、解析状态分布、重复组、失败/卡住文档
.venv/bin/python scripts/weknora_ops.py status

# 输出 JSON / 留档
.venv/bin/python scripts/weknora_ops.py status --json --report /tmp/weknora-status.json

# 清理重复（保留 completed/最新），同时清理解析失败
# 先不加 --apply 看计划
.venv/bin/python scripts/weknora_ops.py cleanup --delete-failed
.venv/bin/python scripts/weknora_ops.py cleanup --delete-failed --apply

# 查看模型
.venv/bin/python scripts/weknora_ops.py models

# 配置摘要模型（先 dry-run，确认后 --apply）
.venv/bin/python scripts/weknora_ops.py summary --model-id <model-id>
.venv/bin/python scripts/weknora_ops.py summary --model-id <model-id> --apply
```

清理规则：

- 只处理标题以 `P0 |` 开头的重复组，避免误删其他知识库内容；
- 每组保留 `parse_status=completed` 优先、`updated_at` 最新的一条；
- `--delete-failed` 删除 `parse_status=failed` 的文档；`pending/processing` 只在 `status` 中列出，不自动删；
- 删除是 WeKnora 异步任务，脚本报告每个 ID 的结果；失败会以非零退出并列出错误。

摘要模型：

- `status` 检查 `summary_model_id` 是否存在、是否在 `GET /api/v1/models` 中；
- 缺失时提示“摘要/自动标签/AI 描述会缺失”；
- `summary --apply` 通过 `PATCH /api/v1/knowledge-bases/{kb}` 写入 `summary_model_id`；
  需要 KB manage 权限，且模型必须已在当前空间创建。

## 4. 权限与审计口径

P0 是 WeKnora 业务知识库的唯一写入方；权限映射：

| P0 角色 | 业务操作 | WeKnora 侧权限 |
| --- | --- | --- |
| admin | 发布、清理、摘要模型、审计 | KB 管理 + API key manage |
| reviewer | 复核后发布 | 由 P0 服务 API key 代写，个人不直接持有 KB 写权限 |
| sales / viewer | 检索/问答（只读） | 由 P0 服务 API key 代查；不发个人 key |
| 运维 | 状态/清理 | 独立 ops key，限制来源 IP，定期轮换 |

- P0 侧发布/问答/审计事件写 `audit_events`（或 JSONL）：`standard_published`、
  `standard_asked`、`task_failed` 等；WeKnora 直接访问只给运维/管理员。
- 生产建议给 ops 脚本单独 API key（需要清理时临时启用 `ingest/manage_kbs`），
  P0 服务 key 只保留发布与检索所需权限。
- 知识库权限映射先以本文为准；后续若开放客户直接访问，再按 WeKnora 空间/租户权限实现。

## 5. 问答评测

脚本：`scripts/run_weknora_eval.py`；数据集：`eval/weknora_qa.demo.json`（22 条，合成标准，随仓库提交）。

```bash
# 本地/CI：不依赖 WeKnora，跑关键词基线
.venv/bin/python scripts/run_weknora_eval.py \
  --dataset eval/weknora_qa.demo.json --mode keyword \
  --report /tmp/eval.json --markdown /tmp/eval.md

# 生产：用已发布 KB + WeKnora
.venv/bin/python scripts/run_weknora_eval.py \
  --dataset eval/weknora_qa.demo.json --from-store --mode weknora \
  --report /tmp/eval-weknora.json
```

- `--from-store` 从 `standard_store.published_kbs()` 取已发布 KB，不再使用数据集里的 `kbs`；
- 用例字段：`expect=hit|refuse`、`standard_no`、`clause`、`tag=direct|paraphrase|out_of_kb`；
- 指标：库内命中率、库外拒答率、引用准确率（标准号+条款）、平均/P95 延迟、按标签拆分；
- `--strict` 任一条失败即退出 1，可用于发布门禁；
- 生产评测数据集应替换为客户真实标准/问法与金标准，当前 demo 数据只验证评测口径，不代表业务指标。

本地 demo 基线（关键词模式，`eval-demo.md`）：

- 22 条：6 条直接命中、8 条同义改写、8 条库外拒答；
- 库内命中 9/14（64.3%），库外拒答 8/8（100%），引用准确 64.3%，平均 0.1 ms；
- 同义改写是关键词基线的弱项；生产应用 WeKnora 混合检索 + 阈值重跑，作为对照。

## 6. 验证

- `tests/test_weknora_ops.py`：同名复用不新建、无匹配才新建、`custom_metadata` 写入、
  清理计划/执行、demo 评测指标、未知标准号+已知条款必须拒答；
- 全量测试：`92 passed, 2 skipped`（本地 PostgreSQL）；
- 发布幂等测试用 `httpx.MockTransport` 模拟 WeKnora API，不访问真实知识库。

## 7. 未完成 / 限制

- **未连接真实 WeKnora**：本工单没有服务器/WeKnora 管理凭据，`status/cleanup/summary`
  未在共享知识库执行；不知道当前是否已有重复/失败记录。上线前请先跑只读 `status` 并留档；
- 生产评测（`--from-store --mode weknora`）需要已发布 KB、API key 与真实问答集；当前只有
  合成 demo 数据集与关键词基线；
- 摘要模型需要在 WeKnora 空间内已创建；`summary --apply` 只改 KB 绑定，不负责创建模型；
- 发布幂等依赖标题稳定；若人工改过 WeKnora 标题，仍需用 `custom_metadata` 或 ops 脚本处理；
- 清理 `failed` 文档会用 WeKnora 异步删除任务，脚本只确认任务已提交，最终删除结果需再跑
  `status` 复核；
- P0 服务与 ops 脚本共用 API key 的方式只适用于内部运维；对外多租户需单独的 WeKnora 权限方案。
