# testkb.yunanht.com 环境备份与中汽知识库开发可行性评估

评估日期：2026-11-24（中国标准时间）

范围：实际由 `testkb.yunanht.com` 提供服务的 WeKnora/DeepKnow 部署；只读环境盘点、备份和隔离数据库恢复演练。未修改服务配置、容器、账号或线上资料。

## 1. 做完了什么

### 1.1 确认域名实际指向

`testkb.yunanht.com` 的 HTTPS 返回 200，页面标题为 DeepKnow。宿主机 Nginx 将域名请求转到 `127.0.0.1:8081`，该端口由 `yunan-persist-frontend-1` 提供；后端 `yunan-persist-app-1` 绑定在宿主机 `127.0.0.1:39200`。因此本报告所称 testkb 是 Compose 项目 `yunan-persist`。

同一主机另有 `rc82kb` WeKnora 栈，前端和后端分别发布在 30901、30900 端口，**不是 testkb 域名当前使用的栈**。曾对该旁路栈生成临时快照，确认域名映射后已将其清理；最终交付的 testkb 备份只对应 `yunan-persist`。

### 1.2 已完成的 testkb 备份

备份位置：`/var/backups/testkb-yunan-persist-20261124`。目录权限为 `0700`，文件权限为 `0600`。包含：

| 文件 | 内容 |
|---|---|
| `database.dump` | PostgreSQL 自定义格式逻辑备份 |
| `files.tar.gz` | WeKnora 原文文件目录 |
| `redis-volume.tar.gz` | Redis 数据卷快照 |
| `config-deployment.tar.gz` | Compose、前端代理配置、`.env` 与 WeKnora 配置 |
| `images.tar.gz` | 实际运行的 app、frontend、docreader、PostgreSQL、Redis 镜像 |
| `ENVIRONMENT-MANIFEST.txt` | 域名、项目名、镜像 ID、容器状态和备份范围 |
| `SHA256SUMS` | 各备份文件校验和 |

验证结果：6 项 SHA-256 全部匹配；四个压缩归档均通过 gzip 完整性检查；PostgreSQL dump 可读取目录清单；使用目标 PostgreSQL 镜像 ID，在 `network=none`、无端口映射的临时容器中实际恢复成功，恢复库的 `pg_tables` 目录查询返回 237 项，临时容器已清理。备份前后 `yunan-persist` 的五个容器均保持运行。

这是同一台主机上的未加密备份，适合本次开发前回滚，不是异地灾备。数据库恢复已演练；全栈恢复、原文文件重新挂载后的业务读取，以及 Redis 恢复尚未演练。数据库、文件和 Redis 是不停机分别快照，不能视为同一事务时点。

## 2. 有没有问题：可行性结论与边界

**结论：有条件可行。** testkb 可作为中汽知识库的短期功能联调/验收运行环境，但不建议直接把当前 `yunan-persist` 栈改成中汽专用环境，也不应把客户真实资料直接放进去。适合度如下：

| 维度 | 现场证据 | 判断 |
|---|---|---|
| 服务可用 | 域名 HTTPS 200；目标五个容器均运行，app、docreader、PostgreSQL 为 healthy | 可做短期联调 |
| 版本与源码 | backend `yunan/kb-app:cn-prompt-main-3`、frontend `yunan/kb-ui:deepknow-main-10`、docreader `v0.9.0-e2v2-rc6`；服务器部署目录没有 Git 工作树 | 运行镜像可用，不能据此复现源码构建或直接作为中汽代码分支 |
| 数据隔离 | 有独立 Compose 项目、PostgreSQL/Redis 卷及原文目录；同一宿主机还运行 auto-ontology、Bisheng3、rc82kb 等项目 | 容器/卷有项目边界，但 CPU、内存、磁盘、Docker 守护进程仍共享 |
| 主机资源 | 8 vCPU、约 30 GiB 内存；评估时可用内存约 19 GiB、根盘可用约 60 GiB（85% 已用），负载较低 | 小规模开发暂可承载；继续堆镜像和数据前应留意磁盘余量，不能据此承诺生产容量 |
| 账号与角色 | OIDC 配置 `enabled=false`；`.env` 中 `DISABLE_REGISTRATION=false`，即当前允许注册 | 不能验收中汽四角色或 SSO；公开测试站不适合存客户数据 |
| 网络与容器权限 | 前端 8081 绑定 `0.0.0.0`；后端 39200 仅绑定 `127.0.0.1`；主机 UFW inactive。外部 HTTPS 可达，外部直连 8081/30900/30901 在本次探测中超时，云侧安全组是否限制仍需云控制台证据 | 不能仅凭本次外部超时认定端口已被可靠隔离；WeKnora app 还挂载了 `/var/run/docker.sock`，对共享主机形成较高权限边界风险 |
| 需求匹配 | WeKnora 能承载知识库、解析、检索和引用；当前站点不是中汽业务系统，也未配置中汽四角色 | 适合作为知识服务，不承担报价、委托、CRM、排期等业务数据源 |

### 使用限制

1. 仅使用合成或经批准脱敏的样例资料，直到确认公网入口访问控制、注册策略、日志/备份保留及客户数据授权。
2. 中汽账号仍按已确认的四个角色设计：新客户/小微客户、业务经理、系统管理员、技术部门。当前 WeKnora 登录和业务工作台权限都未完成该映射，不能把 WeKnora 内置角色直接等同于这四类角色。
3. `yunan-persist` 的 `.env` 启用了注册且 OIDC 关闭；不要把它当作可验收 SSO 的环境，也不要为中汽直接复用现有账号库。
4. app 容器挂载 Docker Socket，且宿主机共享运行多个项目。中汽环境若要存放敏感数据，需先评估移除/代理该能力的影响，并将中汽服务放入独立主机或至少独立受控的运行边界。
5. 备份位于同一主机且未加密；宿主机或磁盘故障会同时影响服务与备份。离线、加密、异地副本和定期恢复演练仍需补齐。

## 3. 下一步建议

1. **先冻结 testkb 当前状态**：不要把当前 `yunan-persist` 目录作为代码工作树；保留本次备份作回滚点。
2. **创建中汽独立运行栈**：新的 Compose project、数据库、原文目录、Redis、镜像标签和访问域名，与 `yunan-persist`、`rc82kb` 分开；先使用合成资料。
3. **代码从可追溯基线构建**：依照[WeKnora 基线与双 Web 整合设计](weknora-source-baseline-and-web-unification-2026-11-24.md)，使用锁定的官方 v0.9.0 源码及经过审查、回到源码树的必要补丁。不要从 `cn-prompt-main-3` 镜像或服务器运行目录推断代码分支。
4. **接入前完成安全门**：关闭公开自助注册；配置并实测 OIDC/账号映射；落实域名/IP 访问限制；评估 Docker Socket 必要性；确认备份加密、异地保留和恢复责任人。
5. **环境验收分开记录**：先测中汽知识库导入、解析、检索、引用和权限隔离；再由业务工作台验收小程序、报价/委托与 CRM 业务流程。testkb 通过只说明知识服务联调环境可用，不代表生产交付验收通过。

当前未对 testkb 做部署、帐号调整、注册开关修改、资料上传或权限迁移。
