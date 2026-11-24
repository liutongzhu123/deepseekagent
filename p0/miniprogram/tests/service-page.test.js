const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const pagePath = path.resolve(__dirname, '../pages/service/index.js');
const serviceApiPath = path.resolve(__dirname, '../utils/service-api.js');

test('failed conversation refresh still restores the pending message for safe retry', async () => {
  const local = new Map([['demo_service_pending_send_v1', {
    registrationId: 'REG-1', text: '请恢复这条未确认消息', conversationId: '', operationId: 'mini-existing-operation',
  }]]);
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: 'http://127.0.0.1:8000' } });
  global.wx = {
    getStorageInfoSync() { return { keys: [...local.keys(), 'demo_registration_capability_v1'] }; },
    getStorageSync(key) {
      if (key === 'demo_registration_capability_v1') return { registrationId: 'REG-1', accessToken: 'opaque' };
      return local.get(key);
    },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
    request(options) { options.fail(new Error('offline')); },
  };
  let definition;
  global.Page = (page) => { definition = page; };
  delete require.cache[serviceApiPath];
  delete require.cache[pagePath];
  require(pagePath);
  const page = {
    data: { messages: [], draft: '', apiEnabled: false, conversationId: '', serviceError: '', sending: false, pendingBlocked: false },
    setData(update) { Object.assign(this.data, update); },
  };
  await definition.refresh.call(page);
  assert.equal(page.data.draft, '请恢复这条未确认消息');
  assert.match(page.data.serviceError, /结果未能确认/);
  assert.equal(page.data.pendingBlocked, false);
});
