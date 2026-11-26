# 中汽 WeKnora 本地模型运行时增量部署

状态：代码与本地门禁已实现；**尚未部署到 testkb**。实际服务器部署和模型拉取须在 PR 合并、备份回读和远端访问恢复后执行。

## 目标拓扑

- 在 /opt/cnz-weknora 项目下新增 cnz-weknora-ollama。
- 只接入 cnz-weknora_WeKnora-network，不发布宿主机端口。
- 模型文件只写入 cnz-weknora_ollama-data，与已有 WeKnora 数据卷和 yunan-persist 分离。
- 使用带 digest 的 Ollama 镜像；不使用已有共享 Ollama 容器、网络或数据卷。
- 保持注册关闭、OIDC 关闭；不导入客户账户或资料。

## 受控部署流程

1. scripts/backup_cnz_weknora_testkb.py --target yunan-101 生成 CNZ-only 快照。脚本支持添加服务前的五容器/四卷状态、添加后的六容器/五卷状态；如此前失败尝试留下专属 Ollama 空卷，也会把它纳入快照。
2. 检查备份目录的 SHA-256 清单、文件权限、数据库 dump 列表和运行版本元数据。
3. scripts/add_cnz_weknora_ollama.py --target yunan-101 --backup <快照路径> 只允许从干净且与远端一致的 main 发布。
4. 预检只接受当前五个 CNZ 容器、四个原有数据卷、schema 138|f、HTTP API/UI 健康及 yunan-persist 五项健康；备份必须指向当前活动/运行源码。
5. 部署器上传经过官方 WeKnora 固定源码与补丁验证的发布包，运行 Compose 隔离契约，再以 up -d --no-deps ollama 单独启动新服务。
6. 部署后核实五个旧容器 ID 不变、四个旧卷保留、只增加专属 Ollama 卷、schema/API/UI/共享栈健康、Ollama 容器只在 CNZ 网络内，最后才切换活动发布链接。
7. 如果启动或验收失败，只删除新增 Ollama 容器，保留其专属卷以便检查；原 CNZ 服务、数据库和卷不回滚/不重建。

## 当前验证边界

- 本地定向测试覆盖部署器、备份兼容、前后端升级守卫及 Ollama 增量发布器：54 项通过。
- 远程部署、镜像中归档工具可用性、实际容器 ID/卷保持、模型下载、WeKnora 模型注册和检索均待 testkb 实测。
- 合成模型仍须先核验许可证与算力后才可纳入评测；当前服务器无 GPU，部署 Ollama 不代表已经选定客户生产模型。
- 客户管理员开户、可信 IdP、OIDC 登录和双 Web 单点流程仍是独立验收门。
