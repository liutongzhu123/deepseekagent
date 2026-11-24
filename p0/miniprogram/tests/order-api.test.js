const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const modulePath = path.resolve(__dirname, '../utils/order-api.js');

function loadClient({ base = 'http://127.0.0.1:8000', capability = null, response = { statusCode: 201, data: { order: { order_id: 'ORDER-DEMO-1' } } }, networkError = false, storageReadFailureKey = '' } = {}) {
  const requests = [];
  const local = new Map();
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: base } });
  global.wx = {
    getRandomValues(options) { options.success({ randomValues: Uint8Array.from({ length: options.length }, (_, index) => index).buffer }); },
    getStorageInfoSync() { return { keys: [...local.keys(), ...(capability ? ['demo_registration_capability_v1'] : [])] }; },
    getStorageSync(key) { if (key === storageReadFailureKey) throw new Error('storage read failed'); return key === 'demo_registration_capability_v1' ? capability : local.get(key); },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
    request(options) {
      requests.push(options);
      if (networkError) options.fail(new Error('network'));
      else options.success(response);
    },
  };
  delete require.cache[modulePath];
  return { client: require(modulePath), requests, local };
}

test('order API stays opt-in and requires registration capability', async () => {
  const { client } = loadClient({ base: '' });
  assert.equal(client.configured(), false);
  await assert.rejects(client.submit({ project_name: 'P' }), /未配置/);
  const active = loadClient();
  await assert.rejects(active.client.list(), /完成本地演示登记/);
});

test('submit forwards registration capability and explicit demo payload', async () => {
  const { client, requests, local } = loadClient({ capability: { registrationId: 'REG-1', accessToken: 'opaque-token' } });
  const order = await client.submit({ project_name: '外观检查', sample_name: '样品 A', quantity: '2 件' });
  assert.equal(order.order_id, 'ORDER-DEMO-1');
  assert.equal(requests[0].url, 'http://127.0.0.1:8000/api/v1/customer-service/demo/orders');
  assert.equal(requests[0].header['X-Demo-Access-Token'], 'opaque-token');
  assert.match(requests[0].header['Idempotency-Key'], /^order-mini-/);
  assert.equal(local.has('demo_order_pending_submit_v1'), false);
  assert.equal(requests[0].data.registration_id, 'REG-1');
});

test('an unreadable pending-operation key blocks order submission before request', async () => {
  const { client, requests, local } = loadClient({
    capability: { registrationId: 'REG-1', accessToken: 'opaque-token' },
    storageReadFailureKey: 'demo_order_pending_submit_v1',
  });
  local.set('demo_order_pending_submit_v1', { malformed: true });
  await assert.rejects(client.submit({ project_name: '项目' }), /本机记录不可读取/);
  assert.equal(client.readPending().unreadable, true);
  assert.equal(requests.length, 0);
});

test('a malformed stored order key blocks submission before request', async () => {
  const { client, requests, local } = loadClient({ capability: { registrationId: 'REG-1', accessToken: 'opaque-token' } });
  local.set('demo_order_pending_submit_v1', {
    registrationId: 'REG-1', payloadFingerprint: JSON.stringify({ template_id: '', project_name: '项目', standard: '', sample_name: '', quantity: '', notes: '' }),
    payload: { template_id: '', project_name: '项目', standard: '', sample_name: '', quantity: '', notes: '' }, operationId: '',
  });
  await assert.rejects(client.submit({ project_name: '项目' }), /操作编号不可验证/);
  assert.equal(client.readPending().unreadable, true);
  assert.equal(requests.length, 0);
});

test('an unreadable pending-update key blocks supplementary submission before request', async () => {
  const { client, requests, local } = loadClient({
    capability: { registrationId: 'REG-1', accessToken: 'opaque-token' },
    storageReadFailureKey: 'demo_order_pending_update_v1',
  });
  local.set('demo_order_pending_update_v1', { malformed: true });
  await assert.rejects(client.update('ORDER-1', { project_name: '项目' }), /本机记录不可读取/);
  assert.equal(requests.length, 0);
});

test('backend and network errors reject without local fallback', async () => {
  const denied = loadClient({
    capability: { registrationId: 'REG-1', accessToken: 'opaque-token' },
    response: { statusCode: 409, data: { detail: '登记尚未批准' } },
  });
  await assert.rejects(denied.client.submit({}), /登记尚未批准/);
  const offline = loadClient({ capability: { registrationId: 'REG-1', accessToken: 'opaque-token' }, networkError: true });
  await assert.rejects(offline.client.submit({}), /结果未能确认/);
});

test('list scopes requests to registration id and returns backend records', async () => {
  const { client, requests, local } = loadClient({
    capability: { registrationId: 'REG 1', accessToken: 'opaque-token' },
    response: { statusCode: 200, data: { orders: [{ order_id: 'ORDER-1' }] } },
  });
  const rows = await client.list();
  assert.deepEqual(rows, [{ order_id: 'ORDER-1' }]);
  assert.match(requests[0].url, /registration_id=REG%201/);
});

test('update uses a scoped PATCH for owner resubmission', async () => {
  const { client, requests, local } = loadClient({
    capability: { registrationId: 'REG-1', accessToken: 'opaque-token' },
    response: { statusCode: 200, data: { order: { order_id: 'ORDER-1', status: 'submitted' } } },
  });
  const order = await client.update('ORDER-1', { template_id: '演示委托模板 A', sample_name: '补充样品' });
  assert.equal(order.status, 'submitted');
  assert.equal(requests[0].method, 'PATCH');
  assert.match(requests[0].url, /registration_id=REG-1/);
  assert.equal(requests[0].header['X-Demo-Access-Token'], 'opaque-token');
  assert.match(requests[0].header['Idempotency-Key'], /^order-update-mini-/);
  assert.equal(local.has('demo_order_pending_update_v1'), false);
});

test('timed-out supplementary update restores original content and reuses its key', async () => {
  const local = new Map();
  const requests = [];
  let committed = false;
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: 'http://127.0.0.1:8000' } });
  global.wx = {
    getRandomValues(options) { options.success({ randomValues: Uint8Array.from({ length: options.length }, (_, index) => index).buffer }); },
    getStorageInfoSync() { return { keys: [...local.keys(), 'demo_registration_capability_v1'] }; },
    getStorageSync(key) { return key === 'demo_registration_capability_v1' ? { registrationId: 'REG-1', accessToken: 'opaque' } : local.get(key); },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
    request(options) {
      requests.push(options);
      if (!committed) { committed = true; options.fail(new Error('response lost after resubmission')); }
      else options.success({ statusCode: 200, data: { order: { order_id: 'ORDER-1', status: 'submitted' } } });
    },
  };
  delete require.cache[modulePath];
  const client = require(modulePath);
  const payload = { template_id: '演示委托模板 A', project_name: '项目', standard: '', sample_name: '样品', quantity: '2', notes: '补件' };
  await assert.rejects(client.update('ORDER-1', payload), /结果未能确认/);
  const pending = local.get('demo_order_pending_update_v1');
  assert.deepEqual(client.readPending('ORDER-1'), { payload, blocked: false, action: 'update', orderId: 'ORDER-1' });
  await assert.rejects(client.update('ORDER-1', { ...payload, notes: '修改内容' }), /按原内容重试/);
  const order = await client.update('ORDER-1', payload);
  assert.equal(order.status, 'submitted');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].header['Idempotency-Key'], requests[1].header['Idempotency-Key']);
  assert.ok(pending && pending.operationId);
  assert.equal(local.has('demo_order_pending_update_v1'), false);
});

test('customer confirmation is explicit, capability-scoped, and server-owned', async () => {
  const { client, requests } = loadClient({
    capability: { registrationId: 'REG-9', accessToken: 'opaque' },
    response: { statusCode: 200, data: { order: { order_id: 'ORDER-9', status: 'customer_confirmed' } } },
  });
  const order = await client.confirm('ORDER-9', 'accepted');
  assert.equal(order.status, 'customer_confirmed');
  assert.equal(requests[0].method, 'POST');
  assert.match(requests[0].url, /ORDER-9\/confirmation\?registration_id=REG-9/);
  assert.deepEqual(requests[0].data, { decision: 'accepted', note: '' });
  assert.equal(requests[0].header['X-Demo-Access-Token'], 'opaque');
});

test('uncertain confirmation can be safely retried with the same terminal decision', async () => {
  const requests = [];
  let committed = false;
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: 'http://127.0.0.1:8000' } });
  global.wx = {
    getRandomValues(options) { options.success({ randomValues: Uint8Array.from({ length: options.length }, (_, index) => index).buffer }); },
    getStorageInfoSync() { return { keys: ['demo_registration_capability_v1'] }; },
    getStorageSync(key) { return key === 'demo_registration_capability_v1' ? { registrationId: 'REG-9', accessToken: 'opaque' } : undefined; },
    request(options) {
      requests.push(options);
      if (!committed) {
        committed = true;
        options.fail(new Error('response lost after confirmation commit'));
      } else {
        options.success({ statusCode: 200, data: { order: { order_id: 'ORDER-9', status: 'customer_confirmed' } } });
      }
    },
  };
  delete require.cache[modulePath];
  const client = require(modulePath);
  await assert.rejects(client.confirm('ORDER-9', 'accepted'), /结果未能确认/);
  const replay = await client.confirm('ORDER-9', 'accepted');
  assert.equal(replay.status, 'customer_confirmed');
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0].data, requests[1].data);
  assert.match(requests[0].url, /ORDER-9\/confirmation/);
});

test('timed-out order submit reuses its operation key and blocks changed content until resolved', async () => {
  const requests = [];
  const local = new Map();
  let committed = false;
  const payload = { template_id: '演示委托模板 A', project_name: '外观检查', sample_name: '样品 A', quantity: '2 件' };
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: 'http://127.0.0.1:8000' } });
  global.wx = {
    getRandomValues(options) { options.success({ randomValues: Uint8Array.from({ length: options.length }, (_, index) => index).buffer }); },
    getStorageInfoSync() { return { keys: [...local.keys(), 'demo_registration_capability_v1'] }; },
    getStorageSync(key) { return key === 'demo_registration_capability_v1' ? { registrationId: 'REG-1', accessToken: 'opaque' } : local.get(key); },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
    request(options) {
      requests.push(options);
      if (!committed) { committed = true; options.fail(new Error('response lost after server commit')); }
      else options.success({ statusCode: 201, data: { order: { order_id: 'ORDER-REPLAY' } } });
    },
  };
  delete require.cache[modulePath];
  const client = require(modulePath);
  await assert.rejects(client.submit(payload), /结果未能确认/);
  const pending = local.get('demo_order_pending_submit_v1');
  assert.ok(pending && pending.operationId);
  assert.deepEqual(client.readPending(), { payload: { ...payload, standard: '', notes: '' }, blocked: false });
  await assert.rejects(client.submit({ ...payload, notes: 'changed' }), /按原内容重试/);
  assert.equal(requests.length, 1);
  const order = await client.submit(payload);
  assert.equal(order.order_id, 'ORDER-REPLAY');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].header['Idempotency-Key'], requests[1].header['Idempotency-Key']);
  assert.equal(local.has('demo_order_pending_submit_v1'), false);
});

test('definitive server rejection clears the pending operation so corrected content can submit', async () => {
  const { client, requests, local } = loadClient({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    response: { statusCode: 400, data: { detail: '样品名称不能为空' } },
  });
  await assert.rejects(client.submit({ project_name: '外观检查', sample_name: '', quantity: '1 件' }), /样品名称不能为空/);
  assert.equal(local.has('demo_order_pending_submit_v1'), false);
  assert.equal(requests.length, 1);
});

test('missing secure random support blocks order submission before any request', async () => {
  const local = new Map();
  let requests = 0;
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: 'http://127.0.0.1:8000' } });
  global.wx = {
    getStorageInfoSync() { return { keys: [...local.keys(), 'demo_registration_capability_v1'] }; },
    getStorageSync(key) { return key === 'demo_registration_capability_v1' ? { registrationId: 'REG-1', accessToken: 'opaque' } : local.get(key); },
    setStorageSync(key, value) { local.set(key, value); },
    request() { requests += 1; },
  };
  delete require.cache[modulePath];
  const client = require(modulePath);
  await assert.rejects(client.submit({ project_name: '项目', sample_name: '样品', quantity: '1' }), /安全随机数/);
  assert.equal(requests, 0);
  assert.equal(local.has('demo_order_pending_submit_v1'), false);
});

test('gateway timeout retains the order operation key for safe replay', async () => {
  const { client, local } = loadClient({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    response: { statusCode: 408, data: { detail: 'gateway timeout' } },
  });
  await assert.rejects(client.submit({ project_name: '外观检查', sample_name: '样品 A', quantity: '2 件' }), /gateway timeout/);
  const pending = local.get('demo_order_pending_submit_v1');
  assert.ok(pending && pending.operationId);
});

test('cleanup confirmation marker blocks new order keys across API reads', async () => {
  const { client, local, requests } = loadClient({ capability: { registrationId: 'REG-1', accessToken: 'opaque' } });
  assert.equal(client.forgetPending(), true);
  assert.equal(client.readPending().needsCheck, true);
  await assert.rejects(client.submit({ project_name: '新项目', sample_name: '样品', quantity: '1' }), /核对服务端委托状态/);
  assert.equal(requests.length, 0);
  assert.equal(local.has('demo_order_pending_submit_v1'), false);
  assert.equal(client.acknowledgeCleanupCheck(), true);
  assert.ok(await client.submit({ project_name: '新项目', sample_name: '样品', quantity: '1' }));
});
