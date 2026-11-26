# 中汽 WeKnora 独立开发栈

该目录只保存上游锁定、项目补丁和环境模板；上游 WeKnora 全源码由校验脚本准备，不把 6,000 多个上游文件复制进本业务仓库。

## 当前锁定版本

- 官方 tag：`v0.9.1`
- commit：`71c32af9273482e0dad013d0e3bea2f4d719516c`
- 官方归档 SHA-256：`183b0538d895b85906da030b8b62a93bd884cd1967d10bcb38020b549b3bc5cd`
- 来源清单：[`ci/upstream-v0.9.1.json`](../ci/upstream-v0.9.1.json)
- 本项目补丁：[`patches/0001-cnz-explicit-oidc-identity.patch`](patches/0001-cnz-explicit-oidc-identity.patch)、[`patches/0002-rustup-mirror.patch`](patches/0002-rustup-mirror.patch)、[`patches/0003-cnz-testkb-build-mirrors.patch`](patches/0003-cnz-testkb-build-mirrors.patch)、[`patches/0004-cnz-knowledge-workspace-profile.patch`](patches/0004-cnz-knowledge-workspace-profile.patch)、[`patches/0005-cnz-login-scope-copy.patch`](patches/0005-cnz-login-scope-copy.patch)、[`patches/0006-cnz-oidc-synthetic-flow-tests.patch`](patches/0006-cnz-oidc-synthetic-flow-tests.patch)、[`patches/0007-cnz-oidc-nonce-binding.patch`](patches/0007-cnz-oidc-nonce-binding.patch)

CNZ 专用前端只保留知识库管理、平台设置和退出入口；通用聊天、Agent、自动化、工具箱、组织与制品页面从导航中隐藏，并由路由白名单阻止直接打开。设置里仅保留模型、解析器、空间与系统管理、任务队列、审计、账号及成员配置；其他设置入口隐藏，直接设置链接也回退到模型页。业务工作台仍是报价、客服与业务知识问答的统一入口；WeKnora 继续承担知识存储、解析和检索。前端隐藏/路由限制只用于精简操作面，后端 API 授权仍是最终安全边界。

## 准备源码

在可信构建机下载官方 GitHub Release Source Code 归档，使用全新空目录运行：

```sh
python3 scripts/prepare_weknora_source.py /path/to/v0.9.1.tar.gz /path/to/new-build-dir
```

脚本拒绝不匹配的归档、损坏或内容不同的清单、危险归档路径，以及非空目标目录；它逐个验证 6,441 个文件后，按顺序检查并应用七个项目补丁。GitHub Actions 也会运行这条准备路径、OIDC 测试、CNZ 前端功能档与登录文案测试。

### 构建网络

构建环境不能直连 Docker Hub 与 `proxy.golang.org`，但 testkb 对项目级 Docker Hub 镜像 `docker.m.daocloud.io`、Go 模块代理 `https://goproxy.cn`、Debian 清华镜像及 Rustup `https://rsproxy.cn` 的探测成功。构建 override 把这些公开端点只传给 CNZ app/frontend/docreader；不修改 Docker daemon 的全局 registry 配置，不影响 `yunan-persist`。Docker Hub 的基础镜像经代理路径获取，已固定 digest 的 Node/nginx 继续按原 digest 拉取；Go migrate 工具从已锁定的主项目 `go.mod`/`go.sum` 安装，不再查询 `@latest`。`APK_MIRROR_ARG` 不带 scheme，`APT_MIRROR` 带 `http://`；选择 HTTP 镜像可在精简基础镜像安装 CA 证书之前下载 apt 索引。`RUSTUP_MIRROR_ARG` 指向提供 `rustup-init.sh`、Rust 工具链和组件的 Rustup 镜像。构建参数是公开镜像地址，不含凭据。

```sh
docker compose -p cnz-weknora \
  -f docker-compose.yml -f docker-compose.testkb.override.yml \
  build app frontend docreader
```

在 `.env` 中设置 `APK_MIRROR_ARG`、`APT_MIRROR`、`GOPROXY_ARG`、`CNZ_DOCKERHUB_MIRROR_ARG`、`NPM_REGISTRY`、`RUSTUP_MIRROR_ARG` 后运行；测试环境模板给出已验证的镜像端点示例。`.env` 仍必须包含独立强随机数据库/Redis 密码并保持 `0600`。

## testkb 环境边界

准备出的源码用于单独的 Compose 项目 `cnz-weknora`，加载 `docker-compose.yml` 和 `docker-compose.testkb.override.yml`。须先复制 `.env.testkb.example` 为构建目录中的 `.env`，生成相互独立的随机数据库与 Redis 密码，设置权限 `0600`，严禁提交；上游 Compose 的 app 使用固定 `env_file: .env`，因此仅向 Compose 命令传 `--env-file` 而没有这个文件是不够的。容器名、project network、数据库/Redis/原文卷和本地端口均与 `yunan-persist` 分开；服务端口只绑定 loopback（18082 / 39301），不更改 testkb 域名 Nginx 路由，不挂载 Docker Socket，不导入现有数据库或资料。

启动前要在 Compose 配置回读中确认项目名、端口、卷、容器名，并在服务器上确认端口未被占用、磁盘余量符合要求。首次管理员开户只能短暂将注册开关改为允许（服务端口仍只绑定 loopback），创建合成管理员账号后，通过 `WEKNORA_BOOTSTRAP_SYSTEM_ADMIN_EMAIL` 完成管理员提升；确认系统管理员可登录后马上将注册改回关闭并重启，再回读有效配置。示例默认关闭 OIDC；只有配置并检查可靠 IdP 的 issuer、client、JWKS、redirect URI 后才能启用，PKCE 保持开启。绑定账户按现有 `user_id` 与 IdP `sub` 逐账号办理。

历史 reviewer 只对原有报告审核/标准复核任务保留隔离；用户已选择按账号确认，确认前不自动映射到中汽业务经理、技术人员或管理员。中汽工作台的四角色授权由其业务服务执行，WeKnora 内置角色仍用于知识资源自身的访问控制。管理员绑定/解除 OIDC 身份前先写入一条不含 subject 的请求审计；审计存储不可用时返回 503 并拒绝变更，成功后再记录完成事件。

## testkb 专用部署器

`scripts/deploy_cnz_weknora_testkb.py --target yunan-101` 只支持首次安装和同一 WeKnora 源码指纹的失败后重试，只能从干净的 `main` 检出运行。脚本优先 fetch `origin/main`；Git smart-HTTP 故障时使用已登录的 `gh` 读取 GitHub main SHA/tree，并要求本地 HEAD 的完整 Git tree 与远端 main tree 完全相同后才继续。脚本会重新校验 WeKnora 官方归档和逐文件清单、应用本仓补丁，再通过 SSH 流式传输到新的版本目录；它不会复制或读取客户知识库资料。运行前目标机必须能以 SSH alias `yunan-101` 登录并使用 Docker，已有的 `/opt/cnz-weknora/active/WeKnora-0.9.1/.env` 必须为 `0600`，testkb 备份校验必须通过，五个 `yunan-persist` 容器必须健康，`/opt` 至少空余 30 GiB。

脚本拒绝覆盖已有版本、运行中的/残留 CNZ 容器或已存在的 active 指针。首次运行时不允许已有 CNZ 数据卷；无容器且未切换 active 指针时，只有当待部署源码与某个 release 中的 WeKnora 源文件指纹完全一致、四个精确卷名齐全且每个卷带有 `cnz-weknora` Compose 所有权标签时，才允许在失败后复用数据卷。源码指纹覆盖相对路径、类型、内容和权限位，排除本地 `.env` 与 `.git`；任何缺卷、多卷、标签不符、源码不同或 active 指针存在都会停止。构建镜像通过后才启动 CNZ Compose 服务；app、PostgreSQL、Redis、docreader 均须 healthy，API/UI 须返回 200 后才原子切换 `/opt/cnz-weknora/active-release`。失败时只停止 `cnz-weknora` 项目并保留其数据卷；任何时候都不执行 `down -v`，不触碰 `yunan-persist`。该脚本支持首次安装与同一 WeKnora 源码指纹的失败后重试，**不能用于升级或数据库回滚**。

已安装 CNZ 栈的纯前端升级使用 `scripts/upgrade_cnz_weknora_frontend.py --target yunan-101`。它重新验证当前 release、备份校验、五个 CNZ 容器/四个卷所有权与健康状态，只构建并替换 frontend。旧前端镜像保留为 `cnz-weknora-ui:rollback-<old-sha>`；新 UI 与 API 检查通过后才切换 active-release，失败时恢复旧镜像和旧指针。该路径不迁移数据库或资料、不重建 app/PostgreSQL/Redis/docreader，也不删除任何卷；仅覆盖前端补丁发布，不是通用版本升级或数据库回滚工具。

后端补丁升级使用 `scripts/upgrade_cnz_weknora_backend.py --target yunan-101 --upstream-archive /path/to/v0.9.1.tar.gz --backup-path /var/backups/cnz-weknora/<verified-snapshot>`。它只接受干净且已提交的本地分支、锁定官方 v0.9.1 归档和有效快照；升级前检查 active 与运行中 app release、配置文件、迁移状态 `138 / clean`、CNZ 容器及卷所有权、服务健康、磁盘余量和五个 `yunan-persist` 服务。它只构建并替换 app，保留当前 app 镜像作为回滚镜像；更新后核对数据库仍为 `138 / clean`、frontend/docreader/PostgreSQL/Redis 容器 ID 和数据卷不变、API/UI 返回 200、OIDC 仍关闭，最后才切换 active-release。失败时尝试用运行中原 release 和原镜像恢复 app；不执行数据库迁移、`down`、卷删除或 `yunan-persist` 操作。它不是完整栈回灌验收，也不启用 OIDC；真实 IdP、账号绑定、检索引用与角色越权验收仍待后续完成。

## testkb CNZ 数据快照

`scripts/backup_cnz_weknora_testkb.py --target yunan-101` 仅备份 CNZ PostgreSQL custom dump、WeKnora 原文卷、docreader 临时卷、Redis 持久卷和版本/镜像/迁移元数据；目录设为 `0700`，文件设为 `0600`，通过 `pg_restore --list`、压缩包可读性和 SHA-256 校验。为取得一致快照，脚本依次短暂停止 CNZ app、docreader、Redis，PostgreSQL 保持在线供 `pg_dump` 使用；退出时按 Redis、docreader、app 顺序尽力逐个恢复，并确认健康检查与 API/UI 200。它只接受 `yunan-101`，校验 CNZ 精确容器/卷、Compose 所属项目标签和实际挂载路径，要求五个 `yunan-persist` 服务健康；不会停止或修改 `yunan-persist`，也不执行删除卷操作。快照目录以原子方式独占创建，避免并发运行共写。它会同时记录活动 release 与正在运行的 app 配置所属 release，前端单独升级造成的版本指针差异不会被隐藏。

快照不复制 `.env`，只记录活动 release 与运行中 app release 的 `.env` 路径和权限；但数据库或持久卷可能包含 WeKnora 保存的连接凭据等应用数据，因此快照仍按敏感数据保护。当前脚本只设置本机目录/文件权限，不提供静态加密；执行前须确认 `/var/backups` 的主机访问与存储保护符合要求。它是 testkb 原机的数据恢复点，不是独立主机迁移包。每次快照会短暂中断 CNZ 知识服务；完整数据恢复演练仍须在隔离副本上完成，单纯校验 dump/tar/SHA 不算恢复验收。

### 2026-11-26 testkb 执行记录

- 有效快照：`/var/backups/cnz-weknora/20261126T052341Z-e87a4c85b02c0963c26ec44ea44696ec1fb36977.QWU7vO`；目录 `0700`、文件 `0600`、SHA-256 校验通过，无 `INCOMPLETE` 标记。
- 数据库恢复演练：使用同版本 PostgreSQL 镜像，在无网络、临时 tmpfs 的隔离容器中恢复，迁移状态为 `138 / clean`，恢复后有 90 张 public 表；临时容器已删除。
- 服务终态：CNZ app、Redis、docreader 健康，API/UI 均返回 200；五个 `yunan-persist` 容器健康。此记录不代表完整应用栈恢复验收：原文/docreader/Redis 归档做了完整性校验，尚未在隔离栈逐项回灌并执行 WeKnora 端到端验收。
- 当时活动 release 为 `e87a4c85...`，运行中 app 配置来自 `9cb69e89...`；快照元数据分别记录两者和配置文件哈希。该差异需在后端升级时结合发布脚本继续核验。
- 目标机 `/var/backups` 所在文件系统为 ext4，脚本没有静态加密。当前归档只靠 root 可访问的目录/文件权限保护，后续需在异机备份前配置加密存储。

部署后仍需按合成资料完成登录/管理员初始化、上传解析、检索引用、OIDC 显式账号绑定及越权拒绝、容器/数据卷隔离核验和备份恢复演练。注册保持关闭，未配置可信 IdP 前 OIDC 保持关闭；因此健康检查成功仅表示运行栈启动，不代表 SSO 或业务端到端验收完成。reviewer 逐账号确认前不迁移任何历史审核权限。
