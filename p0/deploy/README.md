# 部署说明（P0 受控上线 0.5）

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
P0_ADMIN_PASSWORD=<首次启动的管理员密码>
P0_SESSION_SECRET=<随机 48+ 字节>
P0_WEKNORA_BASE_URL=http://127.0.0.1:8081
P0_WEKNORA_API_KEY=<WeKnora tenant API key>
P0_WEKNORA_KB_ID=<P0 业务专用知识库 ID>
P0_WEKNORA_REQUIRED=1
P0_WEKNORA_MIN_SCORE=0.35
P0_WEKNORA_TOP_K=6
P0_WEKNORA_RERANK=0
P0_OCR_ENABLED=1
MAX_UPLOAD_MB=20
MAX_STANDARD_MB=200
```

- 首次启动若没有 `P0_ADMIN_PASSWORD`，会在 `data/auth/initial_admin_password.txt`
  生成一次性密码（0600）；设置环境变量后该文件会被删除。
- `P0_WEKNORA_RERANK=1` 会改用带重排的 `/knowledge-search`，精度更高但明显更慢；
  默认走 `/knowledge-bases/{kb}/hybrid-search`（向量+关键词，约 0.3 秒）。
- 生产不要设置 `P0_AUTH_DISABLED`；该变量仅供 pytest 使用。

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
.venv/bin/python scripts/manage_users.py add <user> <admin|reviewer|viewer>
.venv/bin/python scripts/manage_users.py reset <user>
```

密码只通过交互式 `getpass` 输入，不接受命令行参数。

## 标准发布与 WeKnora

发布接口会：

1. 把复核后的条款渲染为带 `〔标准号 | 条款 | 页码 | 类型 | 项目〕` 引用的 markdown；
2. 写入 WeKnora 的 P0 业务专用知识库（一标准一条 knowledge，重复发布走更新）；
3. 在本地 `data/standards/<id>/kb.json` 保留审计副本与 `weknora.knowledge_id`。

问答只从 WeKnora 检索；`standard_id` 过滤会转成 WeKnora `knowledge_ids`。
WeKnora 不可用且 `P0_WEKNORA_REQUIRED=1` 时返回 503，不会静默改用本地索引。
`mode=local` / `mode=keyword` 仅用于离线调试和前后对比。

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
- 以上均含客户保密材料，禁止复制到仓库或外发。

## Docker（待内部镜像仓库可用）

Docker Hub 当前从该服务器不可达，镜像构建暂未使用。
后续把基础镜像推到 ACR/内部仓库后，用 `docker compose up -d --build` 替换 systemd。
注意 compose 需要把 `.env` 传入容器并保留 `data/` 卷。
