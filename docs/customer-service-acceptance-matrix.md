# 中汽零部件 AI 智能客服需求验收矩阵

**快照日期：** 2026-11-24
**需求来源：** `docs/requirements.yaml` 中 CS-1 至 CS-8 的 29 项条目；范围解释参照 `p0/docs/customer-service-v3-plan.md` 和 `docs/customer-service-development-roadmap.md`。
**状态口径：** 任务 `status=done` 表示本地开发切片完成；需求 `partial` 表示存在可复现的本地/模拟实现，但完整业务验收或生产依赖未满足；`waiting` 表示没有可验收的本地实现或必须先取得外部输入。当前没有任何一项可据此宣称客户 UAT 或生产集成完成。

**本地最终回归（2026-11-24）：** P0 全套 **1120 项通过、41 项跳过、0 项失败**；客服/CRM 专项（CRM、登记、客服队列）**45 项通过**；需求覆盖验证 **18 项通过**；工作台与小程序 Node **66 项通过**。专项数字与全量数字有重叠，不相加。全量有 5 条依赖弃用警告。上述验证覆盖本地 API、模拟数据与用户界面，不覆盖真实客户账号、供应商 API、生产数据库、微信平台或客户 UAT。

## 汇总

- 29 项中：**29 项已有本地部分实现；完整客户需求验收 0 项。**CS-2.6 的本地部分实现只显示显式配置的地区匹配/备用经理候选，实际指派、客户映射和通知仍待外部确认。
- `CS-8.1` 至 `CS-8.4` 的本地故障模拟器只验证应用对规范化结果的分类、幂等冲突和显式回读，不是供应商适配器或真实连接；四项生产对接仍 waiting。
- `CS-2.6` 提供默认关闭的地区路由候选配置和人工复核预览；未配置时不推断，未实现真实指派或通知。
- 生产入口、审批和对客价格规则还需客户字段/权限/签字；测试中的合成企业、价格、联系人和委托不代表客户真实数据或效果。

## 逐项矩阵

| 需求 | 本地状态 | 已有本地实现和验证证据 | 完整验收还缺什么 |
|---|---|---|---|
| CS-1.1 价格库与 CRM 对接同步、管理员维护 | partial | 管理员本地价格库维护页覆盖项目/样品/试验项目/标准/部门/数量/单价/折扣/有效期，冲突须显式覆盖且记录来源/原因；页面保留原始数值文本；价格维护 API 数量/单价/折扣要求十进制字符串并严格解析，拒绝 JSON number、非有限/夹杂字符/浮点下溢、溢出或无法无损往返的精度；CRM 文件同步走独立导入适配；版本/回滚、项目级隔离；`CS_BASE_GOV`、`CS_CRM_SCOPE`、`CS_PRICE_UI`；`p0/tests/test_crm.py` | 客户 CRM API/字段/认证/网络；真实读写同步授权、币种/税/数量口径与对账样例 |
| CS-1.2 排期项目库，人工录入与 AI 识别 | partial | 管理员工作台支持本地排期新增、冲突后显式覆盖、删除，并要求填写来源/原因；新增非 AI 的合成文本确定性候选解析，逐条显示原文/行号，只有管理员人工采用并保存后才进入本地库；异常字段/日期/超长输入不能采用；报价预览保留 CRM 排期来源日期，但在客户确认状态语义前标为未知并要求人工复核；只有明确标记的合成排期可作为演示命中；`CS_BASE_GOV`、`CS_SCHEDULE_EXTRACT_SIM`；`p0/static/tests/schedule-candidate.test.js`、`p0/tests/test_crm.py`、`p0/tests/test_customer_service_preview.py` | 客户排期样例与字段语义；获批 AI/OCR 识别模型、客户样本、字段映射和金标准 |
| CS-1.3 认可清单解析与资质匹配 | partial | 管理员可维护本地资质名称、证书号、范围/有效期/状态原文，来源和原因留审计；重复证书号/名称需显式覆盖；有效期过滤和项目级读取隔离；`CS_BASE_GOV`、`CS_CRM_SCOPE`、`CS_QUAL_UI`；`p0/tests/test_crm.py` | 客户认可清单格式、标准号匹配口径、证书真实性验证方式及签署的项目级/标准级承诺 |
| CS-1.4 项目名称映射库 | partial | 管理员映射维护页新建默认待审；任何业务字段/来源变更后撤销旧批准；审批请求绑定页面所见记录快照，在本地写锁中 CAS 校验，过期时 409；审批接口强制来源/原因；范围隔离且仅批准映射参与匹配；`CS_BASE_GOV`、`CS_CRM_SCOPE`、`CS_NAME_MAPPING_UI`；`p0/tests/test_crm.py` | 客户项目主数据、同义名称、项目归属和有效映射确认 |
| CS-2.1 注册、审批、必填项配置、企查查带入 | partial | 默认关闭的本地注册/补件/状态查询、小程序 opt-in、管理员审核；登记状态读写使用 POSIX 文件锁并覆盖并发注册不丢记录；创建、资料 PATCH、管理员审核均要求 `Idempotency-Key`，同键同载荷重试回放当前登记、同键异文返回 409；资料更新/审核各最多留 100 次摘要，超限拒绝新操作但允许旧请求回放；审核后登记状态变化时旧审核重试不覆盖当前状态或重复追加历史；管理台即使状态筛选改变也保留待恢复审核记录；小程序预存待确认编号/原内容，响应不确定后安全恢复，并隐藏其他登记账号的待处理资料；凭证/待办不可读或结构损坏时停止请求；清理登记待办后用持久化核对标记阻断写入，用户核对并显式确认才解锁；`p0/tests/test_customer_registration.py`、`p0/static/tests/review-queue.test.js`、`p0/miniprogram/tests/registration-api.test.js`、`p0/miniprogram/tests/registration-page.test.js` | 微信登录/手机号授权、客户注册字段、企查查字段、审批定义；多主机生产存储需数据库事务/唯一约束；服务器 `P0_SESSION_SECRET` 需跨重启稳定，轮换后旧待确认 key 无法派生原登记凭证 |
| CS-2.2 中介识别与异常账号人工复核 | partial | 本地关键词候选提示；审核只记人工决定，规则不可用时暂停；`p0/tests/test_customer_registration.py` | 中介判定词表、处置策略、异常账号/人工审核规则签字 |
| CS-2.3 黑名单增删/导入导出/日志 | partial | 管理员可维护名单；提供单列 UTF-8 CSV 合并/替换导入、带公式注入防护的导出；名单写入和整段重置均要求配置版本并在写前审计，CAS 冲突或审计不可用时拒绝写入；非管理员不能读取/改写名单或通过规则预览探测命中；`p0/tests/test_admin_config.py`、`p0/tests/test_customer_business_rules.py`、`p0/tests/test_customer_registration.py` | 客户正式名单来源、黑名单命中处置/留存审批和客户 UAT |
| CS-2.4 白名单/重点客户识别与经理通知 | partial | 精确企业白名单与名称关键词形成管理员侧人工复核候选；会话队列高亮重点客户候选，配置异常时 fail-closed；不自动放行、不通知；`p0/tests/test_customer_business_rules.py`、`p0/tests/test_customer_registration.py`、`p0/tests/test_customer_service_desk.py` | 客户重点客户定义、经理映射、通知渠道和送达验收 |
| CS-2.5 竞业识别 | partial | 本地中介/关键词候选匹配与人工复核边界；`p0/tests/test_customer_business_rules.py` | 客户竞业经营范围词表、企业数据来源与人工决策口径 |
| CS-2.6 按地点匹配经理和备用经理 | partial | 管理员可配置地区→账号及备用账号；审核队列展示精确/备用候选并将规则版本写入内部审核快照；销售及普通配置读取/草稿接口不暴露、不允许改经理映射；不会自动指派或发送通知；`p0/tests/test_customer_region_routing.py`、`p0/tests/test_customer_registration.py`、`p0/tests/test_admin_config.py`、`p0/tests/test_business_rule_versions.py` | 客户确认的地区层级/别名、正式经理与备用名单、跨区/休假规则及通知渠道和送达验收 |
| CS-3.1 按金额走小额自动/大额人工 | partial | 本地询价状态流可转人工；规则预览固定候选，不出报价；`p0/tests/test_customer_quote_flow.py`、`p0/tests/test_customer_business_rules.py` | 金额阈值、币种/税、审批人、自动出价授权及报价验收样例 |
| CS-3.2 上传大纲并自动分析 | partial | Word/Excel/PDF/图片解析、逐行人工修订和来源证据；`p0/tests/test_quote_multiformat.py`、`p0/tests/test_customer_quote_parse_review.py` | 客户文件样本、字段映射和解析金标准 |
| CS-3.3 自然语言交互补全信息 | partial | 本机报价表单可将项目/标准/数量/时效/说明原样带入 opt-in 客服演示服务，客户消息进入管理员会话队列；当前接手管理员可回复，客户刷新回读；没有启用 API 时仍为清楚标记的离线保存；`p0/miniprogram/tests/quote-handoff.test.js`、`p0/tests/test_customer_service_desk.py` | 客户对话语料、必要字段策略、已批准本地模型和回复准确性/业务验收样本 |
| CS-3.4 同时报价格/排期/资质 | partial | 授权 CRM 只读预览可返回价格候选、排期及资质，并要求消歧；`p0/tests/test_customer_service_preview.py` | 客户 CRM 样例/真实字段、有效期口径和完整数据验收 |
| CS-3.5 梯度折扣/加急/高价/缺项等规则 | partial | 规则配置、候选预览、草稿/差异/重放；未签规则不发布、不自动报价；规则测试见 `p0/tests/test_customer_business_rules.py`、`p0/tests/test_business_rule_versions.py` | 各规则的正式语义、折扣顺序、金额边界、审批人和生效期签署 |
| CS-3.6 转人工并保留上下文 | partial | 询价转人工状态、客服会话服务端存档、管理员队列接单/转交/关闭；询价创建及队列动作使用稳定操作号，同键重试回放当前记录（即使状态已变化）、同键异文冲突且不重复追加历史；操作摘要最多 100 条，损坏元数据 fail closed；管理台保存待恢复动作与原载荷，状态筛选不隐藏恢复项；408/425/429 和服务端失败保留原操作号，确定业务拒绝才清理；内部摘要不出现在 API；提交响应不确定时先回读服务端当前状态；数值数量须为小于 10^40 的有限正数，来源行号/置信度严格限型限值；无法取得安全随机数时客服写操作在发请求前停止；本地写入用 POSIX 文件锁防止同机多 worker 覆盖；`p0/tests/test_customer_quote_flow.py`、`p0/tests/test_customer_service_desk.py`、`p0/static/tests/operation-id.test.js`、`p0/static/tests/review-queue.test.js` | 经理角色/在线状态、备用人、真实通知渠道与客户授权；多主机生产存储需数据库事务/唯一约束 |
| CS-4.1 提取标准/条款/项目/样品数量 | partial | 多格式解析、逐行证据、人工修订和迟到响应保护；`p0/tests/test_customer_quote_parse_review.py`、`p0/tests/test_quote_multiformat.py` | 脱敏客户大纲、专家期望结果、客户准确率验收标准 |
| CS-4.2 匹配 CRM 并输出标准 Excel | partial | 只读 CRM 预览与精确绑定人工复核的内部 Excel 草稿；重新读取当前项目 CRM 快照、唯一命中才列价格、导出留审计；`p0/tests/test_customer_service_export.py`、`p0/tests/test_customer_service_preview.py`、`p0/tests/test_quote_multiformat.py` | 客户标准 Excel 模板/字段字典、真实项目数据、写入授权及端到端对账 |
| CS-5.1 多主机厂、自定义委托模板 | partial | A/B 本地演示模板和提交时不可变模板快照；`p0/tests/test_customer_order_flow.py` | 主机厂模板、可配置字段、下拉选项和业务版本审批 |
| CS-5.2 注册信息带入并补充提交 | partial | 已批准的演示登记凭证绑定委托，预填并校验演示字段；创建和补件重提均用登记凭证作用域的 `Idempotency-Key` 去重，同键同内容重放、异文冲突；补件响应丢失恢复原表单/操作号，服务端保留最多 100 次操作摘要以支持多轮后旧键安全回放，登记状态改变后仍可回放，管理员再次审核后旧操作不会回退当前状态；清理本机待办后持久化标记阻断新写入，核对服务端并显式确认后解锁；`p0/tests/test_customer_order_flow.py`、`p0/miniprogram/tests/order-api.test.js` | 正式注册身份/字段、客户必填项及生产账号关联授权 |
| CS-5.3 可配置审核并支持客户确认 | partial | 本地管理员补件/批准/拒绝后，委托所有人可显式接受或暂不接受；管理员审核要求稳定 `Idempotency-Key`，同键同决定/原因/修订号回放当前委托、同键异文冲突，后续补件/重审后旧请求不会覆盖新状态；委托每次客户补件、管理审核或客户确认均递增版本，旧队列审核按修订号 CAS 拒绝并发补件后的审批；小程序在响应不确定时恢复原审核；管理台状态筛选不隐藏待恢复记录；确认只在已批准态可用并审计，同结果重试幂等、相反结果/他人操作拒绝；同机多 worker 使用 POSIX 锁避免互斥终态覆盖；不发邮件、不创建真实任务；`p0/tests/test_customer_order_flow.py`、`p0/static/tests/review-queue.test.js`、`p0/miniprogram/tests/order-api.test.js` | 客户正式审批矩阵、确认/撤回/过期业务语义签字，邮件或微信回执及客户 UAT |
| CS-6.1 经理名下客户咨询/报价/委托工作台 | partial | 管理员演示队列可筛选注册、委托、询价与会话；会话支持状态/企业/重点候选/未读组合筛选，并按当前规则显示内部候选高亮；接单/转交/归档有幂等重试与筛选中恢复；`p0/tests/test_customer_service_desk.py`、`p0/tests/test_customer_order_flow.py`、`p0/static/tests/review-queue.test.js` | 经理客户归属、角色授权、重点客户业务口径和跨流程生产数据汇总 |
| CS-6.2 企微/小程序对话存档、提醒、转交 | partial | 演示会话本人隔离、服务端存档、管理员队列按员工分别计算客户未读；被分配的管理员可幂等回复，客户凭证回读回复，客户响应不暴露员工账号；接单/转交/归档用稳定操作号，同键重试回放当前会话、不重复追加历史、异文冲突；管理台不确定响应保留原动作并支持跨状态筛选恢复；仅确定性应用拒绝码清除待发送操作，网络/408/425/429/服务器错误保留原内容待重试；会话读取失败仍恢复原待确认草稿；清理本机客服待办后持久化标记阻断发送，刷新核对并显式确认后解锁；无外部消息发送；`p0/tests/test_customer_service_desk.py`、`p0/static/tests/review-queue.test.js`、`p0/miniprogram/tests/service-api.test.js`、`p0/miniprogram/tests/service-page.test.js` | 企微/微信 API、合规存档、外部未读通知、附件和真实送达回执；多主机生产存储需数据库唯一约束/事务实现 |
| CS-7.1 部门维护业务规则 | partial | 分区配置、候选预览、草稿版本、差异/重放；管理员配置 CAS 与草稿读写均由本机 POSIX 文件锁保护跨 worker 并发，锁初始化失败时 fail closed，发布端点仍关闭；`p0/tests/test_admin_config.py`、`p0/tests/test_business_rule_versions.py` | 认可的部门边界、审批矩阵、生效时间和规则语义签署；多主机生产存储需数据库事务/唯一约束 |
| CS-7.2 认可清单和 CRM 基础报价维护 | partial | 本地 CRM 文件库和基础数据维护/审计；`p0/tests/test_crm.py` | 客户认可清单格式、CRM 字段、生产同步策略及授权 |
| CS-8.1 企查查 | partial（仅模拟） | `IntegrationSimulator` 覆盖规范化成功/失败分类与显式人工边界；`p0/tests/test_integration_simulator.py` | 采购产品、账号、字段、匹配规则、配额、隐私授权、真实供应商测试 |
| CS-8.2 CRM 读/写/同步 | partial（文件适配/仅模拟） | CRM 文件适配和集成故障模拟器；真实 API 模式 fail closed；`p0/tests/test_crm.py`、`p0/tests/test_integration_simulator.py` | OpenAPI、认证/网络、项目字段、写授权、幂等和远端回读契约 |
| CS-8.3 微信登录/消息/对话 | partial（本地 API/仅模拟） | opt-in 小程序 API 连接和会话模拟；没有微信平台登录/出站；`p0/miniprogram/tests/*.test.js`、`p0/tests/test_integration_simulator.py` | AppID/主体、登录/客服消息权限、签名回调、授权文案、合法域名 |
| CS-8.4 邮件确认/通知 | partial（仅模拟） | 邮件故障分类和 pending→显式回读演练；无 SMTP/API 发送；`p0/tests/test_integration_simulator.py` | 发件服务认证、模板、收件人政策、回执/退信和重试规则 |

## 生产/客户验收阻塞清单

1. **业务与规则责任方：** 客户业务负责人提供并签署折扣/加急/金额阈值、名单/竞业含义、委托模板、审批角色、经理地域映射和生效/撤回规则。
2. **系统责任方：** 客户 IT/供应商提供 CRM、企查查、微信/企微、邮件接口文档、字段字典、账号/测试环境、网络出口与读写授权。
3. **数据与安全责任方：** 客户安全/隐私负责人确认数据分级、存储/留存、日志、备份和出网策略；真实客户数据进入前现场验收本地模型、WeKnora 下游访问及防火墙/DNS/代理。
4. **业务验收责任方：** 客户提供脱敏询价、认可清单、委托与对话样本及专家期望结果，确认准确率、性能和 UAT 签字人。

收到以上输入后，先更新对应版本化契约和合成/脱敏 fixture，再将相应条目从 partial/waiting 推进到供应商级回归与客户 UAT；不得以本地模拟替代这些门槛。
