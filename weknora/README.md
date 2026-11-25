# 中汽 WeKnora 独立开发栈

该目录只保存上游锁定、项目补丁和环境模板；上游 WeKnora 全源码由校验脚本准备，不把 6,000 多个上游文件复制进本业务仓库。

## 当前锁定版本

- 官方 tag：`v0.9.1`
- commit：`71c32af9273482e0dad013d0e3bea2f4d719516c`
- 官方归档 SHA-256：`183b0538d895b85906da030b8b62a93bd884cd1967d10bcb38020b549b3bc5cd`
- 来源清单：[`ci/upstream-v0.9.1.json`](../ci/upstream-v0.9.1.json)
- 本项目补丁：[`patches/0001-cnz-explicit-oidc-identity.patch`](patches/0001-cnz-explicit-oidc-identity.patch)、[`patches/0002-rustup-mirror.patch`](patches/0002-rustup-mirror.patch)

## 准备源码

在可信构建机下载官方 GitHub Release Source Code 归档，使用全新空目录运行：

```sh
python3 scripts/prepare_weknora_source.py /path/to/v0.9.1.tar.gz /path/to/new-build-dir
```

脚本拒绝不匹配的归档、损坏或内容不同的清单、危险归档路径，以及非空目标目录；它逐个验证 6,441 个文件后，按顺序检查并应用两个项目补丁。GitHub Actions 也会运行这条准备路径及 OIDC 测试。

### 构建网络

镜像构建要显式设置 Dockerfile 支持的 Debian 镜像主机名、Go 模块代理、Rustup 分发镜像及 docreader 的 Debian 镜像 URL。`APK_MIRROR_ARG` 不带 scheme，`APT_MIRROR` 带 `http://`；选择 HTTP 镜像可在精简基础镜像安装 CA 证书之前下载 apt 索引。`RUSTUP_MIRROR_ARG` 指向提供 `rustup-init.sh`、Rust 工具链和组件的 Rustup 镜像。构建参数是公开镜像地址，不含凭据。Compose override 将 Debian、Go、Rustup 参数传给 app 构建。

```sh
docker compose -p cnz-weknora \
  -f docker-compose.yml -f docker-compose.testkb.override.yml \
  build app frontend docreader
```

在 `.env` 中设置 `APK_MIRROR_ARG`、`APT_MIRROR`、`GOPROXY_ARG`、`NPM_REGISTRY`、`RUSTUP_MIRROR_ARG` 后运行；测试环境模板给出已验证的镜像端点示例。`.env` 仍必须包含独立强随机数据库/Redis 密码并保持 `0600`。

## testkb 环境边界

准备出的源码用于单独的 Compose 项目 `cnz-weknora`，加载 `docker-compose.yml` 和 `docker-compose.testkb.override.yml`。须先复制 `.env.testkb.example` 为构建目录中的 `.env`，生成相互独立的随机数据库与 Redis 密码，设置权限 `0600`，严禁提交；上游 Compose 的 app 使用固定 `env_file: .env`，因此仅向 Compose 命令传 `--env-file` 而没有这个文件是不够的。容器名、project network、数据库/Redis/原文卷和本地端口均与 `yunan-persist` 分开；服务端口只绑定 loopback（18082 / 39301），不更改 testkb 域名 Nginx 路由，不挂载 Docker Socket，不导入现有数据库或资料。

启动前要在 Compose 配置回读中确认项目名、端口、卷、容器名，并在服务器上确认端口未被占用、磁盘余量符合要求。首次管理员开户只能短暂将注册开关改为允许（服务端口仍只绑定 loopback），创建合成管理员账号后，通过 `WEKNORA_BOOTSTRAP_SYSTEM_ADMIN_EMAIL` 完成管理员提升；确认系统管理员可登录后马上将注册改回关闭并重启，再回读有效配置。示例默认关闭 OIDC；只有配置并检查可靠 IdP 的 issuer、client、JWKS、redirect URI 后才能启用，PKCE 保持开启。绑定账户按现有 `user_id` 与 IdP `sub` 逐账号办理。

历史 reviewer 只对原有报告审核/标准复核任务保留隔离；用户已选择按账号确认，确认前不自动映射到中汽业务经理、技术人员或管理员。中汽工作台的四角色授权由其业务服务执行，WeKnora 内置角色仍用于知识资源自身的访问控制。管理员绑定/解除 OIDC 身份前先写入一条不含 subject 的请求审计；审计存储不可用时返回 503 并拒绝变更，成功后再记录完成事件。

## 发布门槛

PR #238（身份适配）和 PR #239（构建镜像顺序）已合并，GitHub Agent/Python/Go CI 通过。testkb 已准备隔离配置、独立随机密码和已校验备份，尚未启动中汽容器。目标机 app 镜像构建已通过 Debian apt、Go 模块和 DuckDB 下载阶段，当前在两处 Rustup 工具链安装阶段因直连静态分发站无进度而终止；Rustup 镜像配置修复正在开发。待镜像构建成功后，仍须完成 Compose 渲染、loopback 健康检查、独立 PostgreSQL/文件/Redis 数据卷验证、合成资料导入/检索/引用、OIDC 账号绑定和权限拒绝测试，以及停止与回滚演练。绝不以 `docker compose down -v` 清理现存 `yunan-persist`。
