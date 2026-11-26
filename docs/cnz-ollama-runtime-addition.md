# 中汽 WeKnora 本地模型运行时增量部署

状态：PR #254（发布标记修复）与 PR #255（Ollama 网络隔离）已合并；独立 Ollama 容器和 `bge-m3:567m` 已部署，app 容器内部 embedding 调用实测返回 1024 维。最新完整快照已验证。WeKnora app 仍需通过小型部署 PR 更新内部 Ollama 地址并安全重建，待审查后执行；管理员引导也待临时本机入口的明确批准。

## 目标拓扑

- 在 /opt/cnz-weknora 项目下新增 cnz-weknora-ollama。
- 只接入 cnz-weknora_WeKnora-network，不发布宿主机端口。
- app 容器将 `OLLAMA_BASE_URL` 显式设置为 `http://ollama:11434`，在隔离的 Compose 网络内调用 Ollama。
- 模型文件只写入 cnz-weknora_ollama-data，与已有 WeKnora 数据卷和 yunan-persist 分离。
- 使用带 digest 的 Ollama 镜像；不使用已有共享 Ollama 容器、网络或数据卷。
- 保持注册关闭、OIDC 关闭；不导入客户账户或资料。

## 受控部署流程

1. 用 Python 3.12 执行 `uv run --python 3.12 python scripts/backup_cnz_weknora_testkb.py --target yunan-101` 生成 CNZ-only 快照。显式指定版本，避免系统 `python3` 解析到旧版解释器。脚本支持添加服务前的五容器/四卷状态、添加后的六容器/五卷状态；如此前失败尝试留下专属 Ollama 空卷，也会把它纳入快照。
2. 检查备份目录的 SHA-256 清单、文件权限、数据库 dump 列表和运行版本元数据。
3. 用 `uv run --python 3.12 python scripts/add_cnz_weknora_ollama.py --target yunan-101 --backup <快照路径>` 执行部署。它只允许从干净且与远端一致的 main 发布。
4. 预检只接受当前五个 CNZ 容器、四个原有数据卷、schema 138|f、HTTP API/UI 健康及 yunan-persist 五项健康；备份必须指向当前活动/运行源码。
5. 部署器上传经过官方 WeKnora 固定源码与补丁验证的发布包，运行 Compose 隔离契约，再以 up -d --no-deps ollama 单独启动新服务。
6. 部署后核实 app 已重建并读取 Ollama 内部地址，另四个既有容器 ID 不变、四个旧卷保留、只增加专属 Ollama 卷、schema/API/UI/共享栈健康、Ollama 容器只在 CNZ 网络内，最后才切换活动发布链接。
7. 如果 app 重建后未健康，先用旧 release Compose 配置恢复 app。确认旧地址与健康状态恢复后再删除 Ollama 容器；若 app 无法恢复则保留 Ollama 容器和专属卷供排障，既有数据库与数据卷不回滚/不重建。

## 当前验证边界

- 本地定向测试覆盖部署器、备份兼容、前后端升级守卫及 Ollama 增量发布器：55 项通过；PR #255 的 GitHub 三项检查通过。
- 2026-11-26 首次部署前置验收发现 Ollama 与 WeKnora app 间需要显式的内部服务地址。当前增量配置明确设定 `OLLAMA_BASE_URL=http://ollama:11434`，并由 Compose 契约检查；网络仍只限 CNZ WeKnora 专属网络。
- 2026-11-26 Ollama `bge-m3:567m` 下载完成；从 WeKnora app 容器请求 `http://ollama:11434/api/embed` 成功，维度为 1024。注册配置仍为 `invite_only`。创建管理员前的新快照位于 `/var/backups/cnz-weknora/20261126T132615Z-b89348a321188a13d5acee6bf99ee856c2c8350f.2oX0KT`，校验通过。
- 合成模型仍须先核验许可证与算力后才可纳入评测；当前服务器无 GPU，部署 Ollama 不代表已经选定客户生产模型。
- 客户管理员开户、可信 IdP、OIDC 登录和双 Web 单点流程仍是独立验收门。
