# P1-6 运维与安全收口

> 状态：监控指标/告警规则、结构化访问日志、黑盒巡检、备份恢复演练、依赖扫描与
> 等保差距清单已完成本地验证；Prometheus/Alertmanager、异机备份、证书续期监控和
> 等保整改未在共享服务器落地（见 §7）。

## 1. 监控与告警

### 应用指标（`/metrics`）

`app/monitoring.py` 暴露 Prometheus 文本指标：

| 指标 | 含义 |
|---|---|
| `p0_up` / `p0_uptime_seconds` | 进程存活/运行时长 |
| `p0_http_requests_total{method,route,status}` | 请求计数（路由已归一，避免 ID 高基数） |
| `p0_http_request_duration_seconds_sum/count` | 请求耗时累计 |
| `p0_tasks{status}` | 任务队列 queued/running/succeeded/failed/cancelled |
| `p0_store_up` | PostgreSQL 可达性（文件模式恒 1） |
| `p0_disk_used_ratio` / `p0_disk_free_bytes` | P0 数据目录所在磁盘 |
| `p0_backup_age_seconds` | 最新备份年龄；不存在为 -1 |
| `p0_weknora_configured` / `p0_weknora_required` | 知识底座配置状态 |

访问控制：

- `P0_METRICS_TOKEN` 设置时，必须带 `X-Metrics-Token` 或 `?token=`；
- 未设置 token 时只允许 loopback；`P0_ENV=production` 时即 loopback 也拒绝，必须配 token；
- `/api/v1/system/monitor` 是登录后的 JSON 视图（`system:status` 权限），用于人工排查。

Prometheus 示例在 `deploy/monitoring/prometheus.yml`，告警规则在
`deploy/monitoring/p0-alerts.yml`。

### 告警覆盖

| 对象 | 指标/规则 | 阈值 |
|---|---|---|
| 服务 | `P0ServiceDown` / `p0_up` | 2 分钟无抓取 |
| 服务 | `P0High5xxRate` | 5xx 比例 > 2% 持续 5 分钟 |
| 解析/任务 | `P0TaskFailed` / `p0_tasks{status="failed"}` | > 0 持续 1 分钟 |
| 队列 | `P0TaskQueueBacklog` | queued > 10 持续 10 分钟 |
| 磁盘 | `P0DiskHigh` | > 85% 持续 10 分钟 |
| 备份 | `P0BackupStale` | 无备份或 > 26h |
| WeKnora/模型 | `P0WeKnoraMissing` | required=1 且 configured=0 |
| 证书 | `P0CertificateExpiring` | 到期 < 21 天（blackbox exporter） |
| 黑盒 | `P0BlackboxCheckFailed` | `p0_monitor_up=0` |

模型**调用健康**（非仅配置）目前没有独立指标：WeKnora/摘要模型的可用性由
`scripts/weknora_ops.py status` 与评测脚本覆盖；后续接 Prometheus blackbox 或
WeKnora 自己的 `/health` 再补一个 `p0_weknora_up`。

### 黑盒巡检

`scripts/monitor_check.py` 不依赖 Prometheus：

```bash
.venv/bin/python scripts/monitor_check.py \
  --base-url http://127.0.0.1:8090 --metrics-token "$P0_METRICS_TOKEN" \
  --data-dir /opt/cq-ai-p0/data --backup-dir /var/backups/cq-ai-p0 \
  --tls-host <DOMAIN> --strict
```

检查项：健康检查与延迟、`/metrics`、失败/排队任务、DB 可达、磁盘、备份年龄、
TLS 证书剩余天数；`--prometheus` 输出 `p0_monitor_*` 指标，`--strict` 阈值失败退出 1。
`deploy/p0-monitor.service` / `.timer` 每 5 分钟运行一次；可把 `OnFailure=` 接到
飞书 webhook 或 Alertmanager。

## 2. 结构化日志

- `P0_LOG_FORMAT=json` 时根日志使用 JSON 行：`ts/level/logger/message`，访问日志附加
  `request_id/method/path/status/ms/user`；
- 每个响应带 `X-Request-ID`（客户端传入则复用），便于把 nginx/P0/WeKnora 日志串起来；
- 日志不记录请求体、密码、Cookie、文件内容；审计仍走 `audit_events`/JSONL；
- `P0_ACCESS_LOG=0` 可关闭应用访问日志（uvicorn 自己的访问日志另算）。

## 3. 备份与恢复

- 文件数据：`scripts/backup_data.py` / `restore_data.py`（P1-0.5）；
- 数据层：`scripts/backup_store.py` / `restore_store.py`（P1-1）；
- 巡检：`scripts/monitor_check.py` 检查最新备份年龄；
- 恢复演练：`scripts/restore_drill.py --backup-dir ... --restore-files`
  校验文件快照与数据层清单，并把文件快照恢复到临时目录；数据层完整恢复需要
  一次性空数据库（见 P1-1 文档）。

本地演练（2026-11-03）：

```text
file_snapshot: OK files=3 bytes=995
file_restore:  OK files=3
data_layer_snapshot: verified=true (pg dump 17,661 B + object tar)
full store restore: new DB p0_ops_restore -> users=1, tasks=0, audits=2
```

## 4. 依赖与漏洞扫描

`scripts/dependency_scan.py`：

- 有 `pip-audit` 时输出 JSON 漏洞报告（120s 超时）；
- 无/超时则回退 `pip check` + `pip list --outdated`，报告断依赖与可升级包；
- `--strict-vuln` 仅在有漏洞时退出 1，避免把“可升级”误当“有漏洞”。

```bash
.venv/bin/pip install pip-audit
.venv/bin/python scripts/dependency_scan.py --requirements requirements.txt --json
```

本次本地结果（2026-11-03）：

- 升级前：`pip-audit` 报 28 个已知漏洞（python-multipart 12、pytest 2、starlette 14）；
- 处理：`fastapi 0.115.6 → 0.162.0`、`starlette 0.41.3 → 1.10.0`、
  `python-multipart 0.0.20 → 0.0.33`、`pytest 8.3.4 → 9.2.0`；
- 升级后：`pip-audit -r requirements.txt` **0 vulnerabilities**；`pip check` 通过；
  全量测试 `107 passed, 2 skipped`；
- 仍可升级但不影响本次结论的包（uvicorn/SQLAlchemy/psycopg 等）列入常规升级，
  不把“可升级”当作漏洞。

## 5. 等保差距评估（等级待客户确认）

未确认等保等级（二级/三级），以下按常见三级要求列出差距，作为整改输入：

| 控制项 | 现状 | 差距/下一步 |
|---|---|---|
| 身份鉴别 | 账号密码、PBKDF2、失败锁定、8h 会话 | 未接 SSO/MFA；管理员双人复核未实现 |
| 访问控制 | RBAC + 项目行级授权 + 管理台 | 无独立安全管理员角色；WeKnora 权限未与 P0 角色统一 |
| 安全审计 | 请求级 + 语义事件 + 保留期 | 未接 SIEM/异地日志；审计表未做防篡改/WORM |
| 入侵防范 | nginx + 应用校验 | 无 WAF/IPS/主机 HIDS；无漏洞扫描周期 |
| 恶意代码 | 无 | 未部署主机杀毒/文件扫描；上传文件未做病毒扫描 |
| 数据完整性/保密 | HTTPS、权限、备份 | 无字段级加密/密钥管理；备份未强制异机加密 |
| 数据备份 | 每日文件/DB/对象备份 + 演练 | 异机/异地与保留策略未落地；恢复 RTO 未由客户确认 |
| 剩余信息保护 | 退出/会话失效 | 未做磁盘擦除/内存清理审计 |
| 个人信息 | 账号/客户数据最小化 | 未做数据分类分级与脱敏策略 |
| 安全运维 | 手工 SSH、systemd | 无堡垒机/双人运维/变更审批留痕 |
| 应急预案 | 有回滚/恢复脚本 | 未做演练记录归档与通知链路 |
| 物理/云 | 单机 4C/15G | 无高可用；服务器扩容按客户要求暂缓 |

建议顺序：确认等保等级 → 补 WAF/主机安全与异机备份 → 日志集中/SIEM →
SSO/MFA 与安全管理员 → 漏洞扫描/渗透测试 → 整改复测。

## 6. 本地验证

- `/metrics`：无 token 403；带 token 200；含 `p0_up`、请求计数、任务/磁盘/备份/
  `p0_store_up`/`p0_weknora_*`；
- `monitor_check --strict`：service/health/metrics/task/store/disk/backup 全部 PASS；
- `restore_drill`：文件快照校验/恢复 3 files；数据层清单 SHA-256 校验通过；
  全量恢复到新库计数 `users=1/tasks=0/audits=2`；
- `X-Request-ID` 透传；`P0_LOG_FORMAT=json` 输出结构化字段；
- 新增 6 项监控测试；全量测试 `107 passed, 2 skipped`（本地 PostgreSQL）。

## 7. 未完成 / 限制

- **未部署**：Prometheus/Alertmanager、systemd 监控 timer、nginx 指标暴露都未在共享
  服务器安装；以上是配置与本地验证。
- 证书告警依赖 blackbox exporter 或 `monitor_check --tls-host`；nginx 证书续期
  未接入自动告警。
- 模型调用健康没有独立黑盒指标；WeKnora 配置状态有，模型/摘要实际可用性仍需
  `weknora_ops status` 或评测。
- 备份仍在同一台机器；异机/异地、加密、WORM 未落地。
- 等保等级、日志留存、数据分级未由客户确认；WAF/HIDS/堡垒机/SIEM 未部署。
- 依赖扫描的漏洞结果以联网 `pip-audit` 为准；离线服务器只能做 `pip check`/outdated；
  本次已把扫描到的 28 个漏洞升级清零，升级后需在部署环境重跑一次。
- 真实生产告警收件人/飞书 webhook 未配置，`OnFailure` 需要接入。
