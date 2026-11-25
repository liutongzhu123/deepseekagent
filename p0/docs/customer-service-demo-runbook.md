# 智能客服本地演示运行手册

**用途：** 在隔离的开发/测试环境复现客户注册、委托审核/确认、客服会话人工接手，以及可选的 S1 询价预览。本文描述本地演示操作，不是生产部署或客户 UAT 手册。

## 1. 演示边界

- 仅使用虚构客户、合成询价和隔离的 `P0_DATA_DIR`；未经数据负责人批准，不得把真实客户资料、凭证或聊天记录放入演示环境。
- 注册、委托和客服演示 API 默认关闭，且在 `P0_ENV=production` 时拒绝启用。不要为了演示修改线上生产开关。
- 演示登记批准不等于身份核验；演示委托批准/客户确认不等于真实检测任务；客服消息只存入本地队列，不发到微信/企微或通知真实人员。
- 小程序清除本机资料不会删除服务端演示记录。使用独立数据目录重开一套空环境，不要直接删除正在运行服务的数据文件。

## 2. 启动隔离服务

在项目 `p0/` 目录安装固定依赖并启动服务。依赖安装需使用已批准的网络/包源；服务目录外的数据目录用于隔离演示记录。

```bash
cd /path/to/source/p0
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p /tmp/zq-customer-service-demo-data
chmod 700 /tmp/zq-customer-service-demo-data
```

在仓库外创建权限为 `0600` 的环境文件，例如 `/tmp/zq-customer-service-demo.env`。其中管理员密码与会话密钥由操作者在本机安全生成并保管，不粘贴到聊天、命令行参数、Git 或日志：

```bash
P0_ENV=development
P0_AUTH_DISABLED=0
P0_DATA_DIR=/tmp/zq-customer-service-demo-data
P0_ADMIN_USER=admin
P0_ADMIN_PASSWORD='REPLACE_WITH_LOCAL_DEMO_PASSWORD'
P0_SESSION_SECRET='REPLACE_WITH_STABLE_LOCAL_RANDOM_SECRET'
P0_CUSTOMER_DEMO_ENABLED=1
P0_CUSTOMER_ORDER_DEMO_ENABLED=1
P0_CUSTOMER_SERVICE_DESK_DEMO_ENABLED=1
P0_CS_DEMO_ENABLED=1
P0_CS_QUOTE_FLOW_ENABLED=1
```

加载该文件并启动：

```bash
set -a
. /tmp/zq-customer-service-demo.env
set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8090
```

会话密钥在演示登记凭证仍需使用期间必须保持稳定，否则已有演示凭证无法继续用于恢复操作。访问 `http://127.0.0.1:8090/health` 检查服务，浏览器打开 `http://127.0.0.1:8090/` 登录管理员工作台。不要把上述本机服务地址用于线上小程序或真实客户账号。

如需微信开发者工具访问本机服务，在“扩展配置”中设置 `customerServiceDemoApiBase` 为 `http://127.0.0.1:8090`。真机调试需满足微信合法 request 域名和 HTTPS 要求；不要通过关闭生产校验绕过平台限制。导入目录为 `p0/miniprogram/`，使用游客 AppID，演示说明见[小程序演示指南](miniprogram-demo.md)。

## 3. 复现客户服务闭环

1. **登记：** 小程序“我的”页填写虚构企业、联系人和地区，提交登记。
2. **管理员审核：** 工作台进入“权限、审计与规则 → 演示注册审核”，刷新队列并补件/批准/拒绝；每次决定填写原因。批准只解锁该隔离演示登记。
3. **委托：** 小程序业务记录页创建演示委托。管理员在“演示委托审核”查看记录并补件/批准/拒绝。客户只能在批准后明确接受或暂不接受。
4. **客服：** 小程序客服页发送虚构问题，或从询价页转交完整上下文。管理员在“客服会话队列”接单、回复、转交或归档；客户刷新会话读取回复。
5. **可选 S1 预览：** 使用 `p0/demo-data/customer-service/` 中的合成资料，通过工作台的 S1 预览验证唯一匹配、歧义、过期和缺字段分支。它不计算总价、不生成对客报价、不写回 CRM。

每一步均应看到“演示”标识；服务端失败不得宣称已提交或已送达。审核和人工接手仅向管理员开放，不能据此验收正式经理角色、真实通知或生产数据隔离。

## 4. 待确认操作与恢复

- 网络中断、超时、网关错误或服务端 5xx 后，先刷新服务端队列/记录；保持原内容，用页面恢复的同一操作号重试。不要修改内容后再次提交，也不要通过重新安装小程序规避待办。
- 明确的业务拒绝会按页面提示修正后重提；不确定结果须先确认服务端记录。操作号和原表单保存在本机待办中。
- 若本机待办/凭证不可读或损坏，页面会停止写入。用户明确清理待办前会收到“服务端记录不会回退”的说明；清理前先落盘核对标记，API 写入口也会检查该标记。
- 清理后先刷新服务端记录/会话，核实原操作是否已落库，再点击“我已核对……继续”。核对标记保存在小程序本机 Storage，离开页面或重启后仍生效；清不掉标记或存储仍不可读时，不要绕过页面继续写入。
- “清除本机演示数据”只清理该设备上的资料，并不会删除服务端记录。全量清理会先保存登记、委托、客服三类核对标记；若任何标记保存失败，会停止清理并保留登记凭证和本机资料。已保存的部分标记可能让对应页面要求先核对状态。

## 5. 测试与停止

在项目根目录运行小程序与工作台纯 JavaScript 回归：

```bash
node --test p0/miniprogram/tests/*.test.js
node --test p0/static/tests/*.test.js
```

Python API 回归从 `p0/` 目录执行：

```bash
.venv/bin/python -m pytest -q tests
```

停止演示服务后，保留所需日志和测试结果；确定不再需要隔离记录时，先确认对应 `P0_DATA_DIR` 路径，再按组织数据留存策略处理。不要清空共享、生产或客户系统的数据目录。

## 6. 当前不包含

真实 CRM、企查查、微信/企微、邮件连接；生产域名与合法域名验收；身份核验、正式经理角色/通知；多主机事务存储；客户数据权限、留存与备份签字；客户 UAT 和性能验收。取得客户接口契约、测试账号、网络策略、规则签署和脱敏验收样本后，应按[验收矩阵](../../docs/customer-service-acceptance-matrix.md)及[集成契约](../../docs/customer-service-integration-contract.md)单独恢复对应任务。
