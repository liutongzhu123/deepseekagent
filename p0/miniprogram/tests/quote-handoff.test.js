const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const serviceApiPath = path.resolve(__dirname, '../utils/service-api.js');
const handoffPath = path.resolve(__dirname, '../utils/quote-handoff.js');
const input = {
  project: '气味检测', standard: 'STD-DEMO-1', quantity: '3 件',
  urgency: '加急咨询', details: '合成样品，不对外承诺价格',
};

function load({ api = false, capability = null, networkError = false } = {}) {
  const values = new Map();
  const requests = [];
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: api ? 'http://127.0.0.1:8000' : '' } });
  global.wx = {
    getRandomValues(options) { options.success({ randomValues: Uint8Array.from({ length: options.length }, (_, index) => index).buffer }); },
    getStorageInfoSync() { return { keys: [...values.keys(), ...(capability ? ['demo_registration_capability_v1'] : [])] }; },
    getStorageSync(key) {
      if (key === 'demo_registration_capability_v1') return capability;
      return values.get(key);
    },
    setStorageSync(key, value) { values.set(key, value); },
    removeStorageSync(key) { values.delete(key); },
    request(options) {
      requests.push(options);
      if (networkError) options.fail(new Error('offline'));
      else options.success({ statusCode: 201, data: { conversation: { conversation_id: 'CONV-QUOTE-1' } } });
    },
  };
  delete require.cache[serviceApiPath];
  delete require.cache[handoffPath];
  return { handoff: require(handoffPath), requests, values };
}

test('offline quote submission remains a local follow-up request', async () => {
  const { handoff } = load();
  const result = await handoff.submit(input);
  assert.equal(result.destination, 'local');
  assert.equal(result.quote.status, '待业务人员确认');
});

test('configured quote submission enters the server conversation with full context', async () => {
  const { handoff, requests } = load({ api: true, capability: { registrationId: 'REG-1', accessToken: 'opaque-token' } });
  const result = await handoff.submit(input);
  assert.equal(result.destination, 'service');
  assert.equal(result.conversation.conversation_id, 'CONV-QUOTE-1');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].data.registration_id, 'REG-1');
  assert.match(requests[0].data.text, /检测项目：气味检测/);
  assert.match(requests[0].data.text, /检测标准：STD-DEMO-1/);
  assert.match(requests[0].data.text, /加急咨询/);
  assert.match(requests[0].data.text, /不含报价承诺/);
  assert.ok(requests[0].header['Idempotency-Key']);
});

test('configured API failures never save a local quote or report a handoff', async () => {
  const { handoff, values, requests } = load({ api: true, capability: { registrationId: 'REG-1', accessToken: 'opaque-token' }, networkError: true });
  await assert.rejects(handoff.submit(input), /结果未能确认/);
  assert.equal(values.has('demo_quotes_v1'), false);
  await assert.rejects(handoff.handoff(input), /结果未能确认/);
  assert.equal(values.has('demo_messages_v1'), false);
  assert.equal(requests[0].header['Idempotency-Key'], requests[1].header['Idempotency-Key']);
});

test('quote submit and handoff inherit the persistent service cleanup gate', async () => {
  const { handoff, requests, values } = load({ api: true, capability: { registrationId: 'REG-1', accessToken: 'opaque-token' } });
  values.set('demo_service_cleanup_check_v1', { at: Date.now() });
  await assert.rejects(handoff.submit(input), /核对服务端会话/);
  await assert.rejects(handoff.handoff(input), /核对服务端会话/);
  assert.equal(requests.length, 0);
  assert.equal(values.has('demo_quotes_v1'), false);
  assert.equal(values.has('demo_messages_v1'), false);
});

test('quote handoff requires a project before any local or remote write', async () => {
  const { handoff, requests, values } = load({ api: true, capability: { registrationId: 'REG-1', accessToken: 'opaque-token' } });
  await assert.rejects(handoff.handoff({ ...input, project: ' ' }), /检测项目/);
  assert.equal(requests.length, 0);
  assert.equal(values.has('demo_messages_v1'), false);
});
