const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const modulePath = path.resolve(__dirname, '../utils/service-api.js');

function load({ base = 'http://127.0.0.1:8000', capability = null, response = { statusCode: 201, data: { conversation: { conversation_id: 'CONV-1' } } }, networkError = false, storageReadFailureKey = '' } = {}) {
  const requests = [];
  const local = new Map();
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: base } });
  global.wx = {
    getRandomValues(options) { options.success({ randomValues: Uint8Array.from({ length: options.length }, (_, index) => index).buffer }); },
    getStorageInfoSync() { return { keys: [...local.keys(), ...(capability ? ['demo_registration_capability_v1'] : [])] }; },
    getStorageSync(key) {
      if (key === storageReadFailureKey) throw new Error('storage read failed');
      if (key === 'demo_registration_capability_v1') return capability;
      return local.get(key);
    },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
    request(options) {
      requests.push(options);
      if (networkError) options.fail(new Error('offline'));
      else options.success(response);
    },
  };
  delete require.cache[modulePath];
  return { api: require(modulePath), requests, local };
}

test('service API is opt-in and requires a registration capability', async () => {
  const { api } = load({ base: '' });
  assert.equal(api.configured(), false);
  await assert.rejects(api.send('hello'), /未配置/);
  const active = load();
  await assert.rejects(active.api.list(), /完成本地演示登记/);
});

test('first message creates a server-side demo conversation with opaque token', async () => {
  const { api, requests } = load({ capability: { registrationId: 'REG-1', accessToken: 'opaque' } });
  const conversation = await api.send('请咨询检测能力');
  assert.equal(conversation.conversation_id, 'CONV-1');
  assert.equal(requests[0].url, 'http://127.0.0.1:8000/api/v1/customer-service/demo/conversations');
  assert.equal(requests[0].header['X-Demo-Access-Token'], 'opaque');
  assert.match(requests[0].header['Idempotency-Key'], /^mini-/);
  assert.ok(requests[0].header['Idempotency-Key'].length >= 16);
  assert.deepEqual(requests[0].data, { registration_id: 'REG-1', text: '请咨询检测能力' });
});

test('an unreadable pending-message key blocks another message before request', async () => {
  const { api, requests, local } = load({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    storageReadFailureKey: 'demo_service_pending_send_v1',
  });
  local.set('demo_service_pending_send_v1', { malformed: true });
  await assert.rejects(api.send('不能在待办不可读时重发'), /本机记录不可读取/);
  assert.deepEqual(api.readPending(), { text: '', blocked: true, unreadable: true });
  assert.equal(requests.length, 0);
});

test('a malformed stored message key blocks sending before request', async () => {
  const { api, requests, local } = load({ capability: { registrationId: 'REG-1', accessToken: 'opaque' } });
  local.set('demo_service_pending_send_v1', {
    registrationId: 'REG-1', text: 'same text', conversationId: '', operationId: '',
  });
  await assert.rejects(api.send('same text'), /操作编号不可验证/);
  assert.equal(api.readPending().unreadable, true);
  assert.equal(requests.length, 0);
});

test('follow-up message is scoped and service errors do not fall back locally', async () => {
  const { api, requests } = load({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    response: { statusCode: 200, data: { conversation: { conversation_id: 'CONV-2' } } },
  });
  await api.send('下一条消息', 'CONV-2');
  assert.equal(requests[0].method, 'POST');
  assert.match(requests[0].url, /CONV-2\/messages\?registration_id=REG-1/);
  assert.deepEqual(requests[0].data, { text: '下一条消息' });
  assert.ok(requests[0].header['Idempotency-Key']);
  const offline = load({ capability: { registrationId: 'REG-1', accessToken: 'opaque' }, networkError: true });
  await assert.rejects(offline.api.send('未送达'), /结果未能确认/);
});

test('a timed-out committed send retries with the same idempotency key', async () => {
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
      if (!committed) { committed = true; options.fail(new Error('response lost after server commit')); }
      else options.success({ statusCode: 201, data: { conversation: { conversation_id: 'CONV-REPLAY', messages: [] } } });
    },
  };
  delete require.cache[modulePath];
  const api = require(modulePath);
  await assert.rejects(api.send('网络中断后的同一条消息'), /结果未能确认/);
  const conversation = await api.send('网络中断后的同一条消息');
  assert.equal(conversation.conversation_id, 'CONV-REPLAY');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].header['Idempotency-Key'], requests[1].header['Idempotency-Key']);
  assert.equal(local.has('demo_service_pending_send_v1'), false);
});

test('pending message text is hidden from a different registration account', async () => {
  const local = new Map([['demo_service_pending_send_v1', {
    registrationId: 'REG-OLD', text: '旧账号消息不得串号', conversationId: '', operationId: 'mini-old-operation-0001',
  }]]);
  const { api, requests } = load({ capability: { registrationId: 'REG-NEW', accessToken: 'new-token' } });
  global.wx.getStorageSync = (key) => key === 'demo_registration_capability_v1'
    ? { registrationId: 'REG-NEW', accessToken: 'new-token' }
    : local.get(key);
  global.wx.getStorageInfoSync = () => ({ keys: [...local.keys(), 'demo_registration_capability_v1'] });
  global.wx.setStorageSync = (key, value) => local.set(key, value);
  global.wx.removeStorageSync = (key) => local.delete(key);
  global.wx.request = (options) => { requests.push(options); options.success({ statusCode: 201, data: { conversation: { conversation_id: 'CONV-NEW' } } }); };
  assert.deepEqual(api.readPending(), { text: '', blocked: true });
  await assert.rejects(api.send('新消息'), /另一登记账号/);
  assert.equal(requests.length, 0);
  assert.equal(api.discardPending(), true);
  assert.equal(api.readPending().needsCheck, true, 'cleanup marker must be visible after pending deletion');
  await assert.rejects(api.send('新消息'), /核对服务端会话/);
  assert.equal(requests.length, 0, 'marker must prevent a new message key before acknowledgement');
  assert.equal(api.acknowledgeCleanupCheck(), true);
});

test('definitive application rejections clear pending, while uncertain failures retain it', async () => {
  const denied = load({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    response: { statusCode: 400, data: { detail: '消息格式无效' } },
  });
  await assert.rejects(denied.api.send('invalid'), /消息格式无效/);
  assert.equal(denied.local.has('demo_service_pending_send_v1'), false);

  const offline = load({ capability: { registrationId: 'REG-1', accessToken: 'opaque' }, networkError: true });
  await assert.rejects(offline.api.send('same text'), /结果未能确认/);
  assert.equal(offline.local.get('demo_service_pending_send_v1').text, 'same text');

  const gatewayTimeout = load({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    response: { statusCode: 408, data: { detail: 'gateway timeout' } },
  });
  await assert.rejects(gatewayTimeout.api.send('maybe committed'), /gateway timeout/);
  assert.equal(gatewayTimeout.local.get('demo_service_pending_send_v1').text, 'maybe committed');

  const rateLimited = load({
    capability: { registrationId: 'REG-1', accessToken: 'opaque' },
    response: { statusCode: 429, data: { detail: 'rate limited' } },
  });
  await assert.rejects(rateLimited.api.send('retry later'), /rate limited/);
  assert.equal(rateLimited.local.get('demo_service_pending_send_v1').text, 'retry later');
});

test('list and last conversation id are local metadata around server-owned messages', async () => {
  const { api, requests, local } = load({
    capability: { registrationId: 'REG 1', accessToken: 'opaque' },
    response: { statusCode: 200, data: { conversations: [{ conversation_id: 'CONV-3' }] } },
  });
  assert.deepEqual(await api.list(), [{ conversation_id: 'CONV-3' }]);
  assert.match(requests[0].url, /registration_id=REG%201/);
  assert.equal(api.writeLast('CONV-3'), true);
  assert.equal(api.readLast(), 'CONV-3');
  assert.equal(local.get('demo_service_conversation_v1'), 'CONV-3');
});
