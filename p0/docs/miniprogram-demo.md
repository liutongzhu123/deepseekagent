# 客户服务小程序本地演示

源码在 `p0/miniprogram/`，面向微信开发者工具，可用游客 AppID 导入目录并预览。

## 当前流程

- 首页进入检测需求表单；保存后形成一条本机“待业务人员确认”的演示需求。
- 报价表单按标准和数量提示待补字段；信息不足时保留为本机“待补充信息”项，不生成报价金额。
- 报价表单默认将需求保存在本机；显式配置本地客服演示 API 后，可将项目、标准、数量、时效和补充说明作为一条客服消息提交到管理员会话队列，并在客服页回读人工回复。失败时不回退为本机“已提交”，也不生成价格或正式报价。
- “我的”页登记虚构客户资料；委托单确认页自动带入企业和联系人。
- 业务记录页默认创建本机演示委托单（两个虚构模板）；连接 opt-in 委托 API 后，则由已批准的演示登记凭证提交到本地服务端，并可查看审核状态及补件重提。服务端记录不能从本机“清空”按钮删除。
- 客服页默认提供固定的离线指引并将对话保存到本机；连接 opt-in 会话 API 后，报价需求和消息保存到演示服务并进入管理员队列；管理员接单后的回复可由客户刷新回读，不自动回复或通知真实客服。登记创建、委托提交和客服发送统一使用 `wx.getRandomValues` 生成 256-bit 幂等编号；客户端先保存待确认操作和原内容，网络结果不确定时用同一编号重试；无安全随机数能力时不发送写请求。
- 我的页显示访客演示态、模拟数据数量并清理本地数据；清理包含客服待确认重试消息，不删除服务端演示数据。
- 可选注册审核演示：通过小程序 `extConfig.customerServiceDemoApiBase` 配置本地非生产演示服务后，提交虚构资料并查询状态；API 默认为关闭，网络失败不会回退为已提交。访问凭证只保存在本机 Storage，清理本机数据不会删除服务端演示记录。

## 边界

本机访客、报价、离线委托和离线对话仍保存在演示设备 Storage。注册、委托和会话 API 只运行在显式开启的本地/隔离演示环境，且在 `P0_ENV=production` 时强制关闭；管理员审核/接手是流程演示，不创建真实客户账号、检测任务、邮件或客服通知。快捷问题的离线答复是固定文案，不代表真实业务承诺，也不由 AI 或客服人员生成。没有真实注册身份核验、微信登录、OpenID/手机号、正式模板、报价计算、CRM/订单接口、企微消息与客服通知能力。页面持续显示演示提示。勿用真实客户资料进行展示。此版本用于评审交互和后续接口设计，不是可发布的小程序。

## 本地预览

在微信开发者工具中选择“导入项目”，目录选 `p0/miniprogram/`。项目使用 `touristappid`，无需配置真实小程序 AppID。首次预览后可在“我的”页清除本机演示数据。

本地存储逻辑的回归检查（Node.js 内置测试运行器，无需安装 npm 依赖）：

```bash
node --test p0/miniprogram/tests/*.test.js
```

隔离 API 服务启动、管理工作台闭环、待确认操作清理与恢复步骤见[智能客服本地演示运行手册](customer-service-demo-runbook.md)。

连接注册/委托/会话演示 API 前，在微信开发者工具的扩展配置中设置 `customerServiceDemoApiBase`，并将域名加入小程序合法 request 域名；本机 HTTP 地址仅适用于开发者工具调试。服务端设置 `P0_CUSTOMER_DEMO_ENABLED=1`；另需分别设置 `P0_CUSTOMER_ORDER_DEMO_ENABLED=1` 和 `P0_CUSTOMER_SERVICE_DESK_DEMO_ENABLED=1` 才能启用委托与会话 API，且不可使用生产环境。未配置时不会发起请求；已配置但 API 失败时不回退为本机成功。

| 调用方 | 接口 | 用途 |
|---|---|---|
| 小程序 | `POST /api/v1/customer-service/demo/registrations` | 必须携带 `Idempotency-Key`；同键同规范化内容重试返回同一草稿和访问凭证，同键异文冲突 |
| 小程序 | `PATCH /api/v1/customer-service/demo/registrations/{id}` | 草稿或待补件时修改登记内容；必须携带 `Idempotency-Key`，同一 key/内容安全重放，不重复增加修订和历史；小程序保存待确认原资料和 key |
| 小程序 | `POST /api/v1/customer-service/demo/registrations/{id}/submit` | 提交演示审核 |
| 小程序 | `GET /api/v1/customer-service/demo/registrations/{id}` | 通过 `X-Demo-Access-Token` 查询自己的状态 |
| 小程序 | `POST /api/v1/customer-service/demo/orders` | 已批准的演示登记凭证提交委托，必须携带 `Idempotency-Key`；同一 key/内容回读原委托，不同内容冲突；服务端固定演示模板版本快照 |
| 小程序 | `POST /api/v1/customer-service/demo/orders/{id}/confirmation` | 仅该委托所有人可对已批准委托记录接受/暂不接受；不会发邮件或创建真实任务 |
| 小程序 | `GET /api/v1/customer-service/demo/orders?registration_id=...` | 通过登记凭证只查询自己的委托 |
| 小程序 | `GET /api/v1/customer-service/demo/orders/{id}` / `PATCH .../{id}` | 读取自己的委托；待补件时补充并重提，不可修改模板快照 |
| 小程序 | `POST /api/v1/customer-service/demo/conversations` | 需提供 `Idempotency-Key`；新建会话并存档第一条消息，响应说明是否为同内容安全重放，返回 `external_delivery=false` |
| 小程序 | `GET /api/v1/customer-service/demo/conversations?registration_id=...` | 通过登记凭证只查询自己的会话 |
| 小程序 | `POST /api/v1/customer-service/demo/conversations/{id}/messages` | 需提供 `Idempotency-Key`；在未归档会话内追加本人消息；同键同内容重放返回原会话，同键不同内容返回冲突，不发送至外部渠道 |
| 管理工作台 | `GET /api/v1/customer-service/demo/admin/registrations` | 管理员查看/筛选演示队列 |
| 管理工作台 | `POST /api/v1/customer-service/demo/admin/registrations/{id}/review` | 管理员补件、批准或拒绝，必须填写原因 |
| 管理工作台 | `GET /api/v1/customer-service/demo/admin/orders` | 管理员筛选演示委托队列 |
| 管理工作台 | `POST /api/v1/customer-service/demo/admin/orders/{id}/review` | 管理员补件、批准或拒绝，必须填写原因 |
| 管理工作台 | `GET /api/v1/customer-service/demo/admin/conversations?status=...` | 管理员查看/筛选本地演示会话及消息上下文 |
| 管理工作台 | `POST /api/v1/customer-service/demo/admin/conversations/{id}/{claim,assign,archive}` | 管理员接单、转交或归档，必须填写原因 |
| 管理工作台 | `POST /api/v1/customer-service/demo/admin/conversations/{id}/reply` | 仅当前接手管理员可回复；回复只存入本地演示会话，不会发送到微信/企微；同键同内容安全重试 |

服务端将访问凭证以哈希保存，注册文件目录权限为 `0700`、文件为 `0600`；审核仅更新演示记录，不创建员工账号或真实客户授权。非管理员审核返回 403。

注册审核状态流：

| 当前状态 | 客户动作 | 管理员动作 | 终态/结果 |
|---|---|---|---|
| `draft` | 修改或提交 | 不可审核 | `submitted` |
| `submitted` | 查询状态 | 补件、批准、拒绝 | `needs_info` / `approved` / `rejected` |
| `needs_info` | 修改后提交，或原样重提 | 等待客户提交 | `draft` → `submitted` |
| `approved` / `rejected` | 只读查询 | 不可再次审核 | 终态 |

`approved` 只是演示记录状态，不代表身份已核验，也不放行真实业务账号、报价或委托。注册 JSON 的读写使用线程锁和 POSIX `flock`，保护同机多个服务进程；生产多主机需数据库事务/唯一约束。

委托演示状态：`submitted` → 管理员 `needs_info` / `approved` / `rejected`；客户仅可在 `needs_info` 时补充字段并重新提交。管理员批准后客户可显式确认，转为 `customer_confirmed`，或暂不接受，转为 `customer_declined`；其他状态、他人登记凭证和互斥重复决定均拒绝，同一决定重试回读原记录。JSON 状态更新以线程锁和 POSIX `flock` 串行化同机多 worker；不支持多主机并发存储。模板版本快照不可被客户补件修改。委托记录、客户确认、模板和审核结果均为本地演示数据，不会推送 CRM、邮件或检测任务系统，也不代表真实委托生效。

首次委托提交使用登记凭证作用域的 `Idempotency-Key`，服务端将规范化内容哈希与操作指纹一同保存；响应超时时小程序保留该操作和原始演示字段，重新打开表单时恢复内容并复用同一编号。同编号不同内容被拒绝。已明确的 4xx 拒绝会清除待重试编号；网络中断或服务端错误保留操作，提示先刷新/按原内容恢复确认。该恢复草稿包含本机演示表单字段，成功确认或收到明确拒绝后删除。

会话演示状态：`open` → `assigned` → `archived`；管理员也可将 `open` 直接转交/归档。只有接手该会话的管理员可回复，客户凭证只访问所属登记的会话并可刷新获取回复；客户响应隐藏员工用户名、内部状态原因和分派账号。管理员操作原因、负责人变化和状态变化写入内部历史。已归档会话不可继续发送或回复，客户可新建会话。服务端用 POSIX 文件锁串行化本机共享文件的读改写，幂等只适用于单机共享文件环境，不支持多主机/NFS 生产存储。消息不会发送给微信客服、企微或人员通知渠道；队列仅管理员可用，业务经理角色映射待客户确认。

会话发送使用本机待确认消息保存原文和操作编号。网络错误、408/429、未知状态码或服务端错误时保留原文及幂等编号，允许用户按原内容安全重试；仅应用确定性拒绝码（400/401/403/404/409/422）清除待操作编号，以便修正后重新提交。提示会区分“已明确拒绝”与“提交结果未能确认”，不会在后者声称消息未存档。

正式接入前需确定小程序 AppID/主体与登录授权流程、后端网关鉴权、CRM/报价/委托单 API、客服/企微消息渠道、消息存档与权限策略、客户隐私声明及审核资质。
