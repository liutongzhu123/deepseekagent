# 中汽 WeKnora 本地模型运行时增量部署

状态更新（2026-11-27）：PR #254（发布标记修复）、#255（Ollama 网络隔离）、#257（app Ollama 地址、OIDC ORM 列名修正）、#258（bootstrap-aware 备份/升级）、#259（Compose 预检模板转义）和 #260（回滚保留 Ollama 等现有服务）均已合并。干净 main commit `a763d735c68f9e4224c33ca78ee59dafd2ea4099` 已部署为 testkb active release；部署前 CNZ-only 快照为 `/var/backups/cnz-weknora/20261127T015420Z-b89348a321188a13d5acee6bf99ee856c2c8350f.TtTneK`。独立 Ollama 容器和 `bge-m3:567m` 已部署，app 容器内部 embedding 调用实测返回 1024 维。

此前首位系统管理员注册因 ORM 将 OIDC 字段映射成不存在的 `o_id_c_issuer` 列而返回 HTTP 400；schema `138|f` 实际已有 `oidc_issuer`、`oidc_subject` 和唯一索引。PR #257 将字段显式映射到数据库列并已部署。随后使用仅绑定服务器 `127.0.0.1:39302` 的临时引导服务，通过 SSH 转发完成指定管理员注册；数据库回读确认账号存在且 `is_system_admin=true`。引导容器及临时管理员邮箱 override 已删除，主服务仍 `invite_only`、OIDC 仍关闭；过程未保存或回显密码。

2026-11-27 当前运行态回读：active release 为 `a763d735c68f9e4224c33ca78ee59dafd2ea4099`；API/UI=200，schema `138|f`，主服务注册模式 `invite_only`，OIDC=false；首位系统管理员账号 1 条且系统管理员标记为真。`tenants/organizations/knowledge_bases/knowledges/resources/data_sources/chunks/messages` 均为 0 行，没有客户知识资料。临时引导容器已删除；6 个 CNZ 业务容器运行，yunan-persist 5/5 healthy。此前完整 CNZ-only 快照和权限、SHA-256、pg_restore 清单校验已通过。

2026-11-27 隔离恢复演练：数据库 dump 实际恢复到无网络、同版 ParadeDB 临时实例，schema 为 `138|f`，users/knowledge_bases/knowledges/resources/chunks/messages 均为 0；原文与 docreader 归档解压后均为 0 文件；Ollama 归档恢复后可列出 `bge-m3:567m`。新旧 Redis 归档按 AOF 配置恢复后 DB0 均为 2 个 key，而既有记录记载 8 个；未读取 key 内容，在线 Redis 计数尚未核对，因此 Redis 一致性及整栈冷恢复仍未验收。所有临时容器与卷已清理，`yunan-persist` 为 5/5 healthy。

## 目标拓扑

- 在 /opt/cnz-weknora 项目下新增 cnz-weknora-ollama。
- 只接入 cnz-weknora_WeKnora-network，不发布宿主机端口。
- app 容器将 `OLLAMA_BASE_URL` 显式设置为 `http://ollama:11434`，在隔离的 Compose 网络内调用 Ollama。
- 模型文件只写入 cnz-weknora_ollama-data，与已有 WeKnora 数据卷和 yunan-persist 分离。
- 使用带 digest 的 Ollama 镜像；不使用已有共享 Ollama 容器、网络或数据卷。
- 保持注册关闭、OIDC 关闭；不导入客户账户或资料。

## 受控部署流程

1. 用 Python 3.12 执行 `uv run --python 3.12 python scripts/backup_cnz_weknora_testkb.py --target yunan-101` 生成 CNZ-only 快照。显式指定版本，避免系统 `python3` 解析到旧版解释器。备份流程在数据库 dump 前暂停并静默主 app 与 loopback-only bootstrap，完成后按序恢复并验证两个认证入口。
2. 检查备份目录的 SHA-256 清单、文件权限、数据库 dump 列表和运行版本元数据。
3. 用 `uv run --python 3.12 python scripts/add_cnz_weknora_ollama.py --target yunan-101 --backup <快照路径>` 执行部署。它只允许从干净且与远端一致的 main 发布。
4. 预检只接受当前五个 CNZ 容器、四个原有数据卷、schema 138|f、HTTP API/UI 健康及 yunan-persist 五项健康；备份必须指向当前活动/运行源码。
5. 部署器上传经过官方 WeKnora 固定源码与补丁验证的发布包，运行 Compose 隔离契约，再以 up -d --no-deps ollama 单独启动新服务。
6. 部署后核实 app 已重建并读取 Ollama 内部地址，另四个既有容器 ID 不变、四个旧卷保留、只增加专属 Ollama 卷、schema/API/UI/共享栈健康、Ollama 容器只在 CNZ 网络内，最后才切换活动发布链接。
7. 如果 app 重建后未健康，先用旧 release Compose 配置恢复 app。确认旧地址与健康状态恢复后再删除 Ollama 容器；若 app 无法恢复则保留 Ollama 容器和专属卷供排障，既有数据库与数据卷不回滚/不重建。

## 当前验证边界

- 本地定向测试覆盖部署器、备份兼容、前后端升级守卫及 Ollama 增量发布器：55 项通过；PR #255 的 GitHub 三项检查通过。
- 2026-11-26 首次部署前置验收发现 Ollama 与 WeKnora app 间需要显式的内部服务地址。当前增量配置明确设定 `OLLAMA_BASE_URL=http://ollama:11434`，并由 Compose 契约检查；网络仍只限 CNZ WeKnora 专属网络。
- 2026-11-26 Ollama `bge-m3:567m` 下载完成；从 WeKnora app 容器请求 `http://ollama:11434/api/embed` 成功，维度为 1024。最新快照校验通过。临时引导容器采用 `self_serve`；主服务公开配置仍为 `invite_only`。注册曾因 ORM 列名错误返回 400，未创建用户；修正并部署代码前不得重试。成功注册后必须立即删除临时容器，并复核主服务配置和 `yunan-persist` 健康状态。
- PR #257 的 GitHub `pytest`、`agent-tests`、WeKnora Go OIDC 检查全部通过；本地 WeKnora Go 回归也已通过。当前分支 `codex/cnz-bootstrap-aware-backup-upgrade` 增加 bootstrap-aware 备份、应用/临时引导实例同步升级与回滚，以及 loopback Compose 覆盖。最新定向 pytest 为 26 passed；本机 Docker Compose 合并旧项目目录与暂存 sidecar 覆盖的独立 fixture 通过，合并后 7 个 service，bootstrap 仅发布到 `127.0.0.1:39302`。独立安全复核曾发现回滚依赖旧 release 中不存在的 bootstrap 覆盖文件；已改用暂存覆盖文件，并在替换 app 前校验文件、SHA-256 与合并 Compose 配置，复核通过。分支相对 `origin/main` 为 231 insertions / 28 deletions（259 changed lines）；PR/CI 待触发。按用户确认的规则，小于 300 行自动创建并合并，超过 300 行需用户审批。部署器仍只接受干净且与远端一致的 `main`。
- 合成模型仍须先核验许可证与算力后才可纳入评测；当前服务器无 GPU，部署 Ollama 不代表已经选定客户生产模型。
- 客户管理员开户、可信 IdP、OIDC 登录和双 Web 单点流程仍是独立验收门。本次 400 的应用日志已确认密码字段被脱敏为 `***`；脚本不保存密码。
