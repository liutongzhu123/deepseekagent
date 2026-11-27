# 中汽 WeKnora 服务端 API 暴露面清单

日期：2026-11-27

上游基线：WeKnora v0.9.1，归档 SHA-256 `183b0538d895b85906da030b8b62a93bd884cd1967d10bcb38020b549b3bc5cd`
用途：作为下一步服务端功能裁剪和权限验收的路由基线；本文件不代表任何 API 已在 testkb 禁用。

## 结论

目前 `0004-cnz-knowledge-workspace-profile.patch` 只约束前端菜单、页面路由和设置分区。上游 `internal/router/router.go` 仍注册整套 API；已登录用户可以绕过前端直接请求它们。所以下一阶段应在服务器路由层按中汽 profile 关闭无关能力，并通过直接 HTTP 请求验证拒绝结果。不能只靠前端隐藏来宣称功能已裁剪。

在 API 客户端调用、系统设置依赖和身份路径未逐项核对之前，不应删除整个路由组。建议先采用显式路由 allowlist：默认关闭；中汽部署启用；未被列入 allowlist 的认证 API 返回 404/403；健康检查、登录/登出、OIDC、知识库、知识导入/管理、模型/解析设置、审计与系统管理按需开放。每一个放行路由都要有对应角色及租户权限检查。

## 路由注册清单

`router.go` 在全局认证中间件后创建 `/api/v1` 路由组，并逐项注册下列能力。第一列是源码注册函数；“建议”是中汽范围的初步分类，不是已完成的禁用清单。

| 源码注册组 | 初步建议 | 依据与待核对项 |
| --- | --- | --- |
| `RegisterAuthRoutes` | 保留 | 登录、当前用户、登出、邀请及 OIDC 绑定流程；逐项检查公共与已认证 API。 |
| `RegisterTenantRoutes`、`RegisterMyInvitationRoutes` | 保留受限子集 | 工作区、成员、邀请、审计相关；按系统管理员/技术部门权限收窄。 |
| `RegisterKnowledgeBaseRoutes`、`RegisterImageAttrRoutes`、`RegisterKnowledgeBaseActivityRoutes` | 保留 | 中汽知识库创建、配置、图片属性与操作审计。 |
| `serveKBScopedFiles`、`serveMessageScopedFiles`、`serveFilesWithResources`、`servePresignedFiles` | 保留受控文件通道 | 知识文件读取、预览和检索引用依赖；校验签名、租户/KB 范围和来源，不能整组关闭。 |
| `RegisterKnowledgeTagRoutes`、`RegisterKnowledgeRoutes`、`RegisterChunkRoutes` | 保留 | 标签、资料导入/发布、分块审核与维护，是知识库基本工作流。 |
| `RegisterGraphSearchRoutes` | 验证后决定 | 仅当客户知识结构化检索确实启用时保留；先检查实际调用和知识库开关。 |
| `RegisterFAQRoutes` | 验证后决定 | 确认中汽是否采用 WeKnora 内置 FAQ；与业务客服 FAQ 数据源职责需区分。 |
| `RegisterSessionRoutes`、`RegisterChatRoutes`、`RegisterMessageRoutes` | 保守保留，默认仅管理员 | 前端独立聊天可隐藏；但必须先查 P0 检索、引用和 Agent API 是否复用会话/消息服务。另需验证 API key 能力策略。 |
| `RegisterModelRoutes` | 保留受限子集 | 管理员维护 LLM/Embedding/Reranker 与凭据；不应让业务经理或外部客户改动。 |
| `RegisterInitializationRoutes`、`RegisterSystemRoutes`、`RegisterSystemAdminRoutes` | 保留受限子集 | 首次初始化、系统设置、系统管理员与审计；需以管理员/技术角色强制授权。 |
| `RegisterDataSourceRoutes` | 保留必要连接器 | 只开放已批准且实际使用的导入方式；外部数据源连接、凭据和同步任务单独授权。 |
| `RegisterWikiPageRoutes` | 验证后决定 | 判断是否用于项目知识整理；否则不纳入中汽工作台。 |
| `RegisterEvaluationRoutes`、`RegisterChunkerDebugRoutes` | 建议关闭或仅技术人员 | 属于评测/调试面，逐接口确认无生产流程依赖后关闭。 |
| `RegisterSandboxConfigRoutes`、`RegisterMyEnvVarRoutes`、`RegisterMyCredentialVaultRoutes`、`/me/browser*` | 建议关闭 | 沙箱、个人环境变量、凭据库、浏览器扩展不属于中汽业务；需同时核查 WebSocket 和 extension 子路由。 |
| `RegisterCustomAgentRoutes`、`RegisterSkillRoutes`、`RegisterMemoryRoutes`、`RegisterAutomationRoutes` | 建议关闭 | 通用 Agent、技能、记忆、自动化功能目前不在已确认的中汽 WeKnora 运维范围内。 |
| `RegisterMCPServiceRoutes`、`RegisterMCPEndpointRoutes` 与 `/mcp/:endpoint_id` | 建议关闭，除非已批准集成 | MCP 配置、凭据、OAuth 和独立 bearer endpoint 是另一套入口，不能只隐藏 UI。 |
| `RegisterWebSearchRoutes`、`RegisterWebSearchProviderRoutes` | 建议关闭 | 中汽知识问答应使用已发布知识；开放 Web Search 会扩大数据出口和答案来源范围。 |
| `RegisterVectorStoreRoutes`、`RegisterStorageBackendRoutes` | 管理员受限或关闭 | 若部署固定使用本地存储与既定向量库，避免用户切换后端；若运维需修改则仅系统管理员可见。 |
| `RegisterOrganizationRoutes`、`RegisterUserFavoriteRoutes` | 建议关闭 | 多组织共享和收藏不是已定义的四角色业务能力；确认无 KB 分享依赖后再关。 |
| `RegisterIMChannelRoutes`、`RegisterEmbedChannelRoutes` 及公开 `/embed/*` | 建议关闭，除非后续集成明确需要 | 外部渠道/嵌入页面拥有独立认证面；需连同公开入口、发布令牌和资源访问一起处理。 |
| `RegisterWeKnoraCloudRoutes` | 建议关闭 | 云端订阅与本项目私有化部署无关。 |

## 全局路由与绕过面

路由审计不能局限于 `/api/v1`：

- 全局 `Auth` 之前注册了 IM 回调、公开 Embed、MCP bearer 服务、资源授权 URL、沙箱终端/桌面 WebSocket 和本地浏览器 extension/internal 路由；需要分别判断是否保留，且不能误以为它们受普通 JWT RBAC 保护。
- `/health`、登录/注册/OIDC 回调、签名预览/下载等匿名或能力令牌路由，需要逐个保留最小必要集合，并测试无凭据、失效凭据、跨租户凭据。
- `/swagger/*` 仅在非 release 模式注册；中汽部署应确认生产 `GIN_MODE=release`，开发环境也不应暴露凭据或写操作。
- API-key gate 位于 `/api/v1` 组内。中汽裁剪必须同时覆盖 API key 与用户 JWT；不允许新增一个只挡 UI/JWT、但 API key 可达的旁路。
- 上游仍有 CORS `*`。若采用浏览器跨站 Cookie 或扩大公开 Embed，应另设受控 Origin；当前路由剖析不能当作 CORS 验收。

## 服务端裁剪验收门

1. 从锁定归档中枚举最终 HTTP 方法、完整路径、认证类型、角色/租户 guard 与对应 handler；将上表从“初步建议”更新为逐路由决策。
2. 搜索 P0 与 WeKnora 前端中的所有调用点；至少覆盖知识库、知识上传/状态/发布、设置、OIDC、工作台检索/引用和文件预览。
3. 为每个开放路由验证允许角色、越权拒绝、无租户拒绝、跨租户拒绝及 API key 能力；对每个关闭路由验证 JWT、API key、别名/尾斜杠和非标准方法均不能绕过。
4. 在 testkb 以管理员登录后完成合成资料上传、检索和引用；再用四角色账号验证知识库管理、检索和配置权限边界。
5. 部署前完成 CNZ-only 备份；部署后回读活动 release、路由拒绝结果、服务健康及 `yunan-persist` 未变更，并保留回滚入口。

## 当前实现状态

2026-11-27，已合并的 PR #263 增加 `0008-cnz-server-feature-profile.patch`：仅当 `WEKNORA_FEATURE_PROFILE=cnz-knowledge-admin` 时启用服务端路由 allowlist；空值保持上游兼容模式，未知值让服务启动失败。profile 拒绝 WeKnora 自助注册/邀请注册、自动开户、创建/删除租户、跨租户枚举、全租户配额操作及工作站目录/沙箱检测。testkb Compose overlay 已设置该 profile。allowlist 保留登录/OIDC 身份、租户成员、知识库与资料管理、无状态知识检索、模型/初始化/系统管理、文件与知识编辑辅助接口；通用聊天/Agent、自动化、MCP、沙箱、嵌入/IM、网页搜索、云功能等未列入开放集合。PR #263 已通过三项 CI 并合并至 main `1b0b2d4`；合并后只读回读 testkb 仍为旧 release `a763d735`，工作区就绪检查为 `not_ready`，所以该 profile 尚未部署。

## 平台密钥管理与工作区密钥管理（2026-11-27 逐接口跟进）

| 方法与 Gin 路径 | CNZ 决策 | 调用与授权依据 |
| --- | --- | --- |
| `GET /api/v1/system/admin/api-keys` | 关闭 | 列出平台级密钥；前端仅 `PlatformAPIKeys.vue` 使用，中汽设置 profile 已隐藏该页面 |
| `POST /api/v1/system/admin/api-keys` | 关闭 | 创建平台级凭据，超出中汽单工作区的集成需要 |
| `DELETE /api/v1/system/admin/api-keys/:key_id` | 关闭 | 与隐藏的整个平台密钥管理入口一并关闭 |
| `GET/POST /api/v1/tenants/:id/api-keys`、`PUT/DELETE /api/v1/tenants/:id/api-keys/:key_id` | 保留现有权限检查 | 工作区知识服务集成使用的密钥管理，仍需 Auth、Owner 与原有 API-key 能力检查 |

服务端 profile 对平台管理路径精确根节点和带斜杠子路径执行拒绝，避免 `/system` 整组放行造成前后端不一致。本变更关闭的是管理入口，不会自动吊销既有密钥；未迁移或导入旧平台密钥。真实 Gin 测试覆盖三条已注册路由与匿名、模拟 Bearer 头、模拟 API-key 头的九个 404 断言，并确认工作区密钥和账号管理匿名请求仍进入 Auth 返回 401。测试中的凭据是无效合成值；这些断言验证 guard 在 Auth 前执行，不代表有效凭据或真实角色的运行态验收。

## 证据边界

此清单基于固定 v0.9.1 源码的 `internal/router/router.go` 与路由注册函数静态检查，代码图谱检查亦覆盖 52 个 router Go 文件（925 个节点、2,506 条边）。它不是运行态 endpoint 清单，不证明角色权限已符合业务，也没有在 testkb 执行任何请求或修改线上配置。当前活动版本与管理员账号状态另见 `docs/BACKLOG.yaml` 的 `KB_IDENTITY_STACK` 续记。
