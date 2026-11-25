const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const pagePath = path.resolve(__dirname, '../pages/profile/index.js');
const registrationApiPath = path.resolve(__dirname, '../utils/registration-api.js');
const pending = {
  fingerprint: JSON.stringify({ company_name: '待恢复企业', contact_name: '联系人', region: '上海', notes: '' }),
  payload: { company_name: '待恢复企业', contact_name: '联系人', region: '上海', notes: '' },
  operationId: 'registration-mini-test-operation',
};

test('profile page restores a pending registration and warns before clearing its retry key', () => {
  const local = new Map([['demo_registration_pending_create_v1', pending]]);
  let modalContent = '';
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: 'http://127.0.0.1:8000' } });
  global.wx = {
    getStorageInfoSync() { return { keys: [...local.keys()] }; },
    getStorageSync(key) { return local.get(key); },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
    showModal(options) { modalContent = options.content; options.success({ confirm: false }); },
  };
  let definition;
  global.Page = (page) => { definition = page; };
  delete require.cache[registrationApiPath];
  delete require.cache[pagePath];
  require(pagePath);
  const page = {
    data: { ...definition.data },
    setData(update) { Object.assign(this.data, update); },
  };
  definition.onShow.call(page);
  assert.equal(page.data.companyName, '待恢复企业');
  assert.equal(page.data.contactName, '联系人');
  assert.equal(page.data.region, '上海');
  assert.equal(page.data.registrationPending, true);
  definition.clear.call(page);
  assert.match(modalContent, /未确认登记操作的重试编号/);
  assert.equal(local.has('demo_registration_pending_create_v1'), true);
});

test('profile page hides pending registration data owned by a different capability', () => {
  const local = new Map([
    ['demo_registration_capability_v1', { registrationId: 'REG-NEW', accessToken: 'new-token' }],
    ['demo_registration_pending_update_v1', {
      registrationId: 'REG-OLD', fingerprint: 'old', operationId: 'registration-update-operation-old',
      payload: { company_name: '旧账号企业', contact_name: '旧联系人', region: '旧地区', notes: '' },
    }],
  ]);
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: '' } });
  global.wx = {
    getStorageInfoSync() { return { keys: [...local.keys()] }; },
    getStorageSync(key) { return local.get(key); },
    setStorageSync(key, value) { local.set(key, value); },
    removeStorageSync(key) { local.delete(key); },
  };
  let definition;
  global.Page = (page) => { definition = page; };
  delete require.cache[registrationApiPath];
  delete require.cache[pagePath];
  require(pagePath);
  const page = { data: { ...definition.data }, setData(update) { Object.assign(this.data, update); } };
  definition.onShow.call(page);
  assert.equal(page.data.companyName, '');
  assert.equal(page.data.registrationPending, true);
  assert.equal(page.data.registrationPendingBlocked, true);
  assert.notEqual(page.data.contactName, '旧联系人');
});

test('full local reset keeps credentials when cleanup marker persistence fails', () => {
  const local = new Map([
    ['demo_registration_capability_v1', { registrationId: 'REG-1', accessToken: 'opaque' }],
    ['demo_profile_v1', { companyName: '本地客户' }],
  ]);
  let toast = '';
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: '' } });
  global.wx = {
    getStorageInfoSync() { return { keys: [...local.keys()] }; },
    getStorageSync(key) { return local.get(key); },
    setStorageSync(key, value) {
      if (key === 'demo_registration_cleanup_check_v1') throw new Error('storage full');
      local.set(key, value);
    },
    removeStorageSync(key) { local.delete(key); },
    showModal(options) { options.success({ confirm: true }); },
    showToast(options) { toast = options.title; },
  };
  let definition;
  global.Page = (page) => { definition = page; };
  for (const module of [
    registrationApiPath,
    path.resolve(__dirname, '../utils/order-api.js'),
    path.resolve(__dirname, '../utils/service-api.js'),
    pagePath,
  ]) delete require.cache[module];
  require(pagePath);
  const page = { data: { ...definition.data }, setData(update) { Object.assign(this.data, update); }, onShow() { definition.onShow.call(this); } };
  definition.clear.call(page);
  assert.equal(local.has('demo_registration_capability_v1'), true);
  assert.equal(local.has('demo_profile_v1'), true);
  assert.match(toast, /重置未执行/);
});

test('full local reset stops when a later marker cannot be persisted and exposes earlier marker', () => {
  const local = new Map([
    ['demo_registration_capability_v1', { registrationId: 'REG-1', accessToken: 'opaque' }],
    ['demo_profile_v1', { companyName: '本地客户' }],
  ]);
  let toast = '';
  global.getApp = () => ({ globalData: { customerServiceDemoApiBase: '' } });
  global.wx = {
    getStorageInfoSync() { return { keys: [...local.keys()] }; },
    getStorageSync(key) { return local.get(key); },
    setStorageSync(key, value) {
      if (key === 'demo_order_cleanup_check_v1') throw new Error('storage full');
      local.set(key, value);
    },
    removeStorageSync(key) { local.delete(key); },
    showModal(options) { options.success({ confirm: true }); },
    showToast(options) { toast = options.title; },
  };
  let definition;
  global.Page = (page) => { definition = page; };
  for (const module of [
    registrationApiPath,
    path.resolve(__dirname, '../utils/order-api.js'),
    path.resolve(__dirname, '../utils/service-api.js'),
    pagePath,
  ]) delete require.cache[module];
  require(pagePath);
  const page = { data: { ...definition.data }, setData(update) { Object.assign(this.data, update); }, onShow() { definition.onShow.call(this); } };
  definition.clear.call(page);
  assert.equal(local.has('demo_registration_capability_v1'), true);
  assert.equal(local.has('demo_profile_v1'), true);
  assert.equal(local.has('demo_registration_cleanup_check_v1'), true);
  assert.equal(local.has('demo_order_cleanup_check_v1'), false);
  assert.equal(page.data.registrationPendingClearedNeedsCheck, true);
  assert.match(toast, /重置未执行/);
});
