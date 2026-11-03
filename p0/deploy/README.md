# 部署说明（P0 受控上线 0.5 + P1-S1 身份/权限/审计 0.6 + P1-1/P1-2 数据层与异步任务 0.7.3 + 报告审核台 0.8.0）

当前 P0 服务器（`<P0_HOST>`，真实地址只在工单/服务器本地）使用 **venv + systemd** 运行：
`/opt/cq-ai-p0`，服务名 `cq-p0-api`，监听 `127.0.0.1:8090`。
对外入口是同一域名的 HTTPS 路径 `/p0/`，由 nginx 反向代理并剥离前缀：

```
https://<DOMAIN>/p0/          -> 127.0.0.1:8090/
https://<DOMAIN>/             -> WeKnora 管理端
```

## 必填环境变量

`/opt/cq-ai-p0/.env`（0600，systemd `EnvironmentFile` 读取，不入库）：

```bash
P0_ENV=production
P0_ROOT_PATH=/p0
P0_COOKIE_SECURE=1
P0_DATA_DIR=/opt/cq-ai-p0/data
P0_ADMIN_USER=admin
P0_ADMIN_PASSWORD=<首次启动的管理员密码；至少 10 位且字母/数字/符号至少两类>
P0_SESSION_SECRET=<随机 48+ 字节>
P0_WEKNORA_BASE_URL=http://127.0.0.1:8081
P0_WEKNORA_API_KEY=<WeKnora tenant API key>
P0_WEKNORA_KB_ID=<P0 业务专用知识库 ID>
P0_WEKNORA_REQUIRED=1
P0_WEKNORA_MIN_SCORE=0.35
P0_WEKNORA_TOP_K=6
P0_WEKNORA_RERANK=0
P0_OCR_ENABLED=1
P0_AUDIT_RETENTION_DAYS=180
MAX_UPLOAD_MB=20
MAX_STANDARD_MB=200

# P1-1 数据层（迁移完成前保持 P0_STORE=json）
P0_STORE=json
# P0_DATABASE_URL=postgresql+psycopg://p0:<password>@127.0.0.1:5432/p0
# P0_OBJECT_STORE=minio
# P0_MINIO_ENDPOINT=127.0.0.1:9000
# P0_MINIO_ACCESS_KEY=<minio access key>
# P0_MINIO_SECRET_KEY=<minio secret key>
# P0_MINIO_BUCKET=cq-p0
# P0_MINIO_SECURE=0
# P0_STORE_MIRROR=1

# P1-2 异步任务（先完成 P1-1 迁移再打开）
# P0_TASKS_ENABLED=1
# P0_TASKS_CONCURRENCY=1
# P0_TASKS_PARSE_ASYNC=1
# P0_TASKS_PUBLISH_ASYNC=1
# P0_TASKS_ASYNC_MIN_MB=5
# P0_TASKS_LEASE_SECONDS=900

# P1-5 CRM 只读适配（先用客户导出，接口就绪后替换）
# P0_CRM_MODE=off
# P0_CRM_PATH=/opt/cq-ai-p0/data/crm/export.xlsx
# P0_CRM_REQUIRED=0
# P0_CRM_API_BASE_URL=
# P0_CRM_API_KEY=

# P1-6 监控与结构化日志
# P0_METRICS_TOKEN=<random token for /metrics; unset = loopback only>
# P0_LOG_FORMAT=json
# P0_ACCESS_LOG=1
# P0_MONITOR_TLS_HOST=<DOMAIN>
```

- `P0_STORE=postgres` 的完整迁移/对账/回滚步骤见 `docs/p1-data-layer/README.md`；
  迁移完成前不要打开该开关。
- `P0_TASKS_ENABLED=1` 需要 `P0_STORE=postgres` 与可写对象存储；worker 在 API 进程内，
  默认并发 1。任务接口与前端进度、取消/重试见 `docs/p1-async-tasks/README.md`。
- 首次启动若没有 `P0_ADMIN_PASSWORD`，会在 `data/auth/initial_admin_password.txt`
  生成一次性密码（0600）；设置环境变量后该文件会被删除。
- `P0_WEKNORA_RERANK=1` 会改用带重排的 `/knowledge-search`，精度更高但明显更慢；
  默认走 `/knowledge-bases/{kb}/hybrid-search`（向量+关键词，约 0.3 秒）。
- 生产不要设置 `P0_AUTH_DISABLED`；该变量仅供 pytest 使用。

## OCR 系统依赖

扫描件 OCR 用本机 ONNX 推理，不依赖云 OCR。`opencv-python` 需要系统 GL 库，
新服务器首次部署时执行：

```bash
apt-get update
apt-get install -y libgl1 libglib2.0-0
cd /opt/cq-ai-p0
.venv/bin/pip install -r requirements.txt
```

装完确认 OCR 引擎可加载（首次加载模型约 0.3 秒）：

```bash
.venv/bin/python -c "import cv2, onnxruntime; from rapidocr_onnxruntime import RapidOCR; RapidOCR(); print('ocr ok')"
```

实测数据（耗时、置信度、复核项、并发限制）见 `docs/ocr-scan-validation/README.md`。

## 更新代码与重启

```bash
# 本地
tar czf - -C p0 app static tests scripts requirements.txt README.md \
  | ssh -i <deploy-key>.pem root@<P0_HOST> 'tar xzf - -C /opt/cq-ai-p0'

# 服务器
cd /opt/cq-ai-p0
.venv/bin/python -m pytest -q tests
systemctl restart cq-p0-api
curl -s http://127.0.0.1:8090/health
```

## 登录与四链路自检

```bash
BASE=https://<DOMAIN>/p0
# 密码从标准输入读取，不进入 shell history
read -rs PW
curl -s -c /tmp/p0.cookies -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}" "$BASE/api/v1/auth/login"

curl -s -b /tmp/p0.cookies "$BASE/api/v1/auth/me"
curl -s -b /tmp/p0.cookies "$BASE/api/v1/standards"
curl -s -b /tmp/p0.cookies -F file=@/tmp/report.docx "$BASE/api/v1/reports/audit"
curl -s -b /tmp/p0.cookies -F file=@/tmp/dvp.xlsx -F template=@/tmp/sales-template.xlsx \
  "$BASE/api/v1/quotes/generate"
curl -s -b /tmp/p0.cookies -H 'Content-Type: application/json' \
  -d '{"question":"<问题>","standard_id":"<已发布标准 ID>"}' "$BASE/api/v1/standards/ask"
```

未登录访问页面会 303 到 `/p0/login`，访问 `/api/*` 返回 401；退出登录用
`POST /p0/api/v1/auth/logout`。

## 用户管理

```bash
cd /opt/cq-ai-p0
.venv/bin/python scripts/manage_users.py list
.venv/bin/python scripts/manage_users.py add <user> <admin|reviewer|sales|viewer>
.venv/bin/python scripts/manage_users.py reset <user>
```

密码只通过交互式 `getpass` 输入，不接受命令行参数；新密码至少 10 位且包含字母/数字/符号中的至少两类。
账号连续 5 次登录失败会锁定 15 分钟，管理员可在 `权限与审计` 页签或重置密码接口解锁。
`projects` 为空的账号不限制项目；填写项目列表后只能访问对应项目标签的标准/报价。

## 标准发布与 WeKnora

发布接口会：

1. 把复核后的条款渲染为带 `〔标准号 | 条款 | 页码 | 类型 | 项目〕` 引用的 markdown；
2. 写入 WeKnora 的 P0 业务专用知识库（一标准一条 knowledge，重复发布走更新）；
3. 在本地 `data/standards/<id>/kb.json` 保留审计副本与 `weknora.knowledge_id`。

问答只从 WeKnora 检索；`standard_id` 过滤会转成 WeKnora `knowledge_ids`。
WeKnora 不可用且 `P0_WEKNORA_REQUIRED=1` 时返回 503，不会静默改用本地索引。
`mode=local` / `mode=keyword` 仅用于离线调试和前后对比。

发布幂等与元数据：本地 `knowledge_id` 失效时按确定性标题查找并更新已有知识；
发布后写 `custom_metadata`（P0 标准 ID/标准号/修订/项目）。WeKnora 运营与评测：

```bash
cd /opt/cq-ai-p0
# 只读体检（推荐每次发布/清理前后各跑一次并留档）
.venv/bin/python scripts/weknora_ops.py status --report /tmp/weknora-status.json
# 清理重复 + 解析失败（先看计划，确认后再 --apply）
.venv/bin/python scripts/weknora_ops.py cleanup --delete-failed
.venv/bin/python scripts/weknora_ops.py cleanup --delete-failed --apply
# 摘要模型：先看了模型列表，再 dry-run / apply
.venv/bin/python scripts/weknora_ops.py models
.venv/bin/python scripts/weknora_ops.py summary --model-id <model-id> --apply
# 评测（--from-store 使用已发布 KB；--mode weknora 需要服务端配置）
.venv/bin/python scripts/run_weknora_eval.py \
  --dataset eval/weknora_qa.demo.json --from-store --mode weknora \
  --report /var/backups/cq-ai-p0/eval-weknora.json
```

清理默认只处理 `P0 |` 开头的重复组，保留 `completed`/最新的一条；详见
`docs/p1-weknora-ops/README.md`。

工作台 UAT（本地/测试环境，不上生产）：

```bash
cd /opt/cq-ai-p0
P0_UAT_PASSWORD='<admin password>' \
  .venv/bin/python scripts/uat_smoke.py \
    --base-url http://127.0.0.1:8090 \
    --manifest /path/to/uat-manifest.json \
    --report /var/backups/cq-ai-p0/uat-report.json \
    --markdown /var/backups/cq-ai-p0/uat-report.md --strict
```

清单格式见 `scripts/uat_manifest.example.json`；真实样例与报告留在工单/本地，不提交仓库。

## nginx

`/etc/nginx/sites-enabled/cq-ai-p0` 中需要：

```nginx
location = /p0 { return 301 /p0/; }
location /p0/ {
    proxy_pass http://127.0.0.1:8090/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /p0;
    client_max_body_size 200m;
    proxy_read_timeout 900s;
}
```

修改后 `nginx -t && systemctl reload nginx`。证书沿用同域名 Let's Encrypt，
HTTP 自动跳 HTTPS。

## 审计与数据

- 业务 API 与登录事件：`/opt/cq-ai-p0/data/audit/audit.jsonl`（JSONL，按行追加）；
- 用户与密码哈希：`data/auth/users.json`（0600）；会话密钥：`data/auth/session_secret`；
- 解析原件、复核结果、导出、报价：`data/standards/`、`data/quotes/`；
- 管理端规则配置：`data/config/rules.json`（0600，随每日快照一起备份）；
- `P0_STORE=postgres` 后，同一批数据的主副本在 PostgreSQL 与 MinIO；
  JSON 树是迁移期镜像/回滚点，仍会同步写入；对象存储的本地目录模式在 `data/objects/`；
- 以上均含客户保密材料，禁止复制到仓库或外发。

## CI 与数据备份（P1-0.5 / P1-1）

- CI：`.github/workflows/p0-ci.yml`，push/PR 到 `main` 且改动 `p0/**` 时跑 pytest；
  P1-1 起附带 PostgreSQL 16 service，集成测试通过 `P0_TEST_DATABASE_URL` 运行；
- 每日文件快照：`p0-backup.timer`（03:30，`Persistent=true`）调用
  `scripts/backup_data.py`，默认写到 `/var/backups/cq-ai-p0`，保留 14 份；
- 数据层快照：`p0-store-backup.timer`（03:40）调用 `scripts/backup_store.py`，
  产出 `pg_dump` + 对象 tar + SHA-256 清单；恢复用 `scripts/restore_store.py`；
- 恢复：`scripts/restore_data.py` 先 `--verify-only` 校验，再恢复到空目录或
  `--force` 替换（旧目录保留为 `data.pre-restore-<时间戳>`）；
- 安装、异机同步、恢复演练与回滚步骤见 `docs/p1-ci-backup/README.md` 与
  `docs/p1-data-layer/README.md`；
  备份归档含账号哈希、会话密钥和客户数据，权限 0600，必须异机保存且限制访问。

## 监控、日志与安全（P1-6）

```bash
# .env
P0_METRICS_TOKEN=<random>
P0_LOG_FORMAT=json
P0_ACCESS_LOG=1
P0_MONITOR_TLS_HOST=<DOMAIN>

# 安装黑盒巡检 timer（每 5 分钟）
cp deploy/p0-monitor.service deploy/p0-monitor.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now p0-monitor.timer
systemctl list-timers 'p0-*'
```

- `/metrics` 生产必须带 `X-Metrics-Token`；未配置 token 时生产环境拒绝（本地 loopback 调试除外）；
- Prometheus 抓取与告警规则示例见 `deploy/monitoring/prometheus.yml`、
  `deploy/monitoring/p0-alerts.yml`；证书告警需 blackbox exporter；
- `monitor_check.py --strict` 失败退出非零，`p0-monitor.service` 的 `OnFailure=` 可接
  飞书 webhook/Alertmanager；
- 结构化日志按 `P0_LOG_FORMAT=json` 输出，`X-Request-ID` 贯通 nginx/P0；
- 恢复演练 `scripts/restore_drill.py`、依赖扫描 `scripts/dependency_scan.py`，
  等保差距见 `docs/p1-ops-security/README.md`。

## Docker（待内部镜像仓库可用）

Docker Hub 当前从该服务器不可达，镜像构建暂未使用。
后续把基础镜像推到 ACR/内部仓库后，用 `docker compose up -d --build` 替换 systemd。
注意 compose 需要把 `.env` 传入容器并保留 `data/` 卷。
