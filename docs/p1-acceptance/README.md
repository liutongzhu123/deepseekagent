# P1 验收与 Gate 2 交付清单

> 基线：`main` @ PR #28 合并后（0.8.3 + 发布/保留/验收检查；SSO 在 0.8.4 接入）。
> 本文回答三个问题：**P1 现在能验收到哪一步、还缺什么、下一步需要谁拍板**。
> 不替代客户签署的验收单，也不把“接口 200”当成“业务已验证”。

## 1. 当前状态

| 项 | 状态 | 依据 |
| --- | --- | --- |
| 应用版本 | 0.8.4（SSO 增量；main 已含 PR #23–#28） | `/health` / `app/main.py` |
| 生产环境 | 仍可能低于 main；本工单无服务器权限，未执行割接 | 本工单历次报告 |
| 自动化测试 | 带临时 PostgreSQL `166 passed, 2 skipped` | `pytest -q` |
| 发布包/检查 | `release_kit.py` build/verify/check 可用 | `docs/p1-release/README.md` |
| 账号/权限 | 四角色 + 项目隔离 + 审计；OIDC 登录已实现（默认关闭，真实 IdP 待接） | `docs/p1-s1-identity`、`docs/p1-sso` |
| 数据层 | PostgreSQL + 对象存储 + 迁移/回滚脚本（本地演练） | `docs/p1-data-layer` |
| 异步任务 | OCR/解析/发布队列、重试/取消/恢复 | `docs/p1-async-tasks` |
| 业务集成 | CRM Excel/CSV 只读 + 对账；真实 API 未接 | `docs/p1-crm-adapter` |
| WeKnora | 发布幂等/运营脚本/评测；生产知识库未在本工单体检 | `docs/p1-weknora-ops` |
| 工作台 | 报告（历史/批量/导出）、标准（下架/修订/回滚）、报价（历史/下载）、问答（反馈/引用跳转） | 对应 `docs/p1-*` |
| 运维安全 | 指标/告警/日志/备份演练/依赖扫描（服务器侧未安装） | `docs/p1-ops-security` |
| 交付材料 | 发布手册、割接/回滚、验收检查脚本；培训/客户验收单未完成 | `docs/p1-release`、本目录 |

## 2. 交付矩阵（已合并 PR）

| 能力 | 版本/PR | 证据 |
| --- | --- | --- |
| 身份、RBAC、项目授权、全量审计 | 0.6.0 / #7 | `docs/p1-s1-identity`（Before/After） |
| 工作台可视化与可操作性 | #8 | `docs/p1-ui-polish` |
| 标准章节匹配放宽与人工复核 | #9 | `docs/loose-heading-match` |
| 下一步 P1 计划 | #10 | `docs/p1-next-plan.md` |
| CI 与文件数据备份基线 | #11 | `docs/p1-ci-backup` |
| 管理端规则配置 / 页内规则 / 配置入口 | 0.7.0–0.7.2 / #12 #14 #15 | `docs/p1-admin-config`、`docs/p1-inline-rules`、`docs/p1-config-entry` |
| 多格式报价 | #13 | `docs/quote-multiformat` |
| 数据层 PostgreSQL + MinIO | P1-1 / #17 | `docs/p1-data-layer` |
| OCR/解析/发布异步任务 | P1-2 / #18 | `docs/p1-async-tasks` |
| WeKnora 发布幂等、运营、评测 | P1-3 / #19 | `docs/p1-weknora-ops` |
| 工作台 UAT 与真实样例 | P1-4 / #20 | `docs/p1-uat` |
| CRM/任务单/样品只读 + 报价对账 | P1-5 / #21 | `docs/p1-crm-adapter` |
| 运维监控/日志/备份/依赖扫描 | P1-6 / #22 | `docs/p1-ops-security` |
| 报告审核历史、批量、项目隔离 | 0.8.0 / #23 | `docs/p1-report-workbench` |
| 标准下架、修订记录、回滚 | 0.8.1 / #24 | `docs/p1-standard-governance` |
| 报价历史与重新下载 | 0.8.2 / #25 | `docs/p1-quote-history` |
| 问答反馈、引用跳转 | 0.8.3 / #26 | `docs/p1-qa-feedback` |
| 版本化发布包与割接/回滚手册 | #27 | `docs/p1-release` |
| 审计导出流式化、任务清理、验收检查 | #28 | `docs/p1-ops-retention`、本目录 |
| 企业 SSO（OIDC）登录 | 0.8.4 / 本 PR | `docs/p1-sso` |

## 3. 自动化验收

```bash
cd p0
# 全量回归（带临时 PostgreSQL；CI 同口径）
P0_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/p0_test pytest -q

# 发布后只读验收（登录 → 身份 → 主要读接口 → 管理员审计/反馈 → 退出）
.venv/bin/python scripts/acceptance_check.py \
  --base-url https://<DOMAIN>/p0 --expect-version 0.8.3 \
  --username admin --report /var/backups/cq-ai-p0/acceptance-$(date +%F).md

# 版本/进程/指标/磁盘/备份/证书
.venv/bin/python scripts/release_kit.py check \
  --base-url https://<DOMAIN>/p0 --expect-version 0.8.3 --strict
.venv/bin/python scripts/monitor_check.py --base-url http://127.0.0.1:8090 \
  --metrics-token "$P0_METRICS_TOKEN" --data-dir /opt/cq-ai-p0/data \
  --backup-dir /var/backups/cq-ai-p0 --strict
```

`acceptance_check.py` 覆盖：`/health` 版本与 auth、登录、`/auth/me`、标准/报告/报价列表、
管理员审计事件/问答反馈/系统状态、退出。以下仍必须人工：五页签交互、上传/复核/发布/导出、
真实样例与 WeKnora 引用、业务判定口径。

## 4. 已知缺口与阻塞

| 缺口 | 影响 | 需要谁 |
| --- | --- | --- |
| 生产未部署到 0.8.3 | 新能力只在代码/测试环境 | 有服务器权限的运维（按 `docs/p1-release` 执行） |
| 生产 Prometheus/告警/备份 timer 未装 | 故障发现与恢复仍靠人工 | 运维 |
| WeKnora 生产知识库未体检/清理 | 重复或失败记录可能影响问答 | 知识库管理员 |
| 5 份标准金标准、20–30 份报告、2–3 个报价案例未齐 | 解析/审核/报价只能做结构验收 | 客户/业务专家 |
| CRM/任务单/样品真实 API 未接 | 报价只能到草稿+Excel 对账 | 客户接口与字段契约 |
| SSO 已实现但未接真实 IdP | 登录页入口可用，生产登录仍用本地账号 | 客户 IdP/账号方案（注册回调地址并验收） |
| 模型网关/多模型路由未做 | 当前问答走 WeKnora、无直连 LLM；做之前需定接入范围 | 项目负责人（是否本期需要） |
| 报价审批前确认未做 | 销售报价下载前无审批流 | 业务负责人（是否本期需要） |

## 5. Gate 2 / Go-No-Go 检查清单

- [ ] 生产 `/health` 版本 = 本次发布版本，`auth=enabled`；
- [ ] `acceptance_check.py` 全 PASS，`monitor_check --strict` 全 PASS；
- [ ] 发布前代码/数据备份存在，恢复演练通过；
- [ ] 五页签、四角色权限（viewer 403）、审计查询/导出人工走查通过；
- [ ] 真实标准/报告/报价样例各至少 1 个，引用带标准号/条款/页码；
- [ ] 已知缺陷清单与回滚命令随发布记录归档；
- [ ] 客户/业务专家对判定口径签字（本工单无法替代）。

## 6. 建议下一步

1. **先合并本 PR 并部署 0.8.3**（发布/回滚手册已就绪）——这是当前最大阻塞；
2. 用真实样例跑一轮 `docs/p1-uat` 与 `acceptance_check`，产出带客户签字的验收记录；
3. 由负责人决定是否本期做 **SSO 实际接入 / 模型网关 / 报价审批**——三者都需要外部输入或产品口径，
   不建议在未确认前继续堆代码；
4. 客户数据到位后补 CRM API、标准金标准与报告样例，再进入 P1 最终验收。
