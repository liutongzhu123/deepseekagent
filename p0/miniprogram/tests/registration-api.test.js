const assert = require('node:assert/strict');
const registrationApi = require('../utils/registration-api');

const storage = new Map();
let apiBase = '';
let serverRegistration = null;
let failRequests = false;
let registrationOperationId = '';
let registrationCreateCount = 0;
let loseCreateResponseOnce = false;
let registrationUpdateOperationId = '';
let registrationUpdatePayload = null;
let registrationUpdateCount = 0;
let loseUpdateResponseOnce = false;
let randomCounter = 0;
const requestLog = [];
let storageReadFailureKey = '';

global.getApp = () => ({ globalData: { customerServiceDemoApiBase: apiBase } });
global.wx = {
  getStorageInfoSync() { return { keys: [...storage.keys()] }; },
  getStorageSync(key) { if (key === storageReadFailureKey) throw new Error('storage read failed'); return storage.get(key); },
  setStorageSync(key, value) { storage.set(key, value); },
  removeStorageSync(key) { storage.delete(key); },
  getRandomValues({ length, success }) {
    const bytes = new Uint8Array(length);
    randomCounter += 1;
    bytes.forEach((_, index) => { bytes[index] = (index + randomCounter) & 255; });
    success({ randomValues: bytes.buffer });
  },
  request(options) {
    requestLog.push({ method: options.method, url: options.url, headers: options.header });
    if (failRequests) return options.fail(new Error('offline'));
    if (options.method === 'POST' && options.url.endsWith('/registrations')) {
      const operationId = options.header['Idempotency-Key'];
      if (!serverRegistration || registrationOperationId !== operationId) {
        registrationCreateCount += 1;
        registrationOperationId = operationId;
        serverRegistration = { registration_id: 'REG-DEMO-1', status: 'draft', ...options.data };
      }
      if (loseCreateResponseOnce) {
        loseCreateResponseOnce = false;
        return options.fail(new Error('response lost after server commit'));
      }
      return options.success({ statusCode: 201, data: { demo: true, registration: serverRegistration, access_token: 'one-time-token' } });
    }
    if (options.method === 'PATCH') {
      const operationId = options.header['Idempotency-Key'];
      if (registrationUpdateOperationId) {
        if (registrationUpdateOperationId !== operationId || JSON.stringify(registrationUpdatePayload) !== JSON.stringify(options.data)) {
          return options.success({ statusCode: 409, data: { detail: '登记更新操作编号冲突' } });
        }
      } else {
        registrationUpdateOperationId = operationId;
        registrationUpdatePayload = options.data;
        registrationUpdateCount += 1;
        serverRegistration = { ...serverRegistration, ...options.data, status: 'draft' };
      }
      if (loseUpdateResponseOnce) {
        loseUpdateResponseOnce = false;
        return options.fail(new Error('response lost after update commit'));
      }
      return options.success({ statusCode: 200, data: { demo: true, registration: serverRegistration } });
    }
    if (options.url.endsWith('/submit')) {
      serverRegistration = { ...serverRegistration, status: 'submitted' };
      return options.success({ statusCode: 200, data: { demo: true, registration: serverRegistration } });
    }
    if (options.method === 'GET') {
      return options.success({ statusCode: 200, data: { demo: true, registration: serverRegistration } });
    }
    return options.success({ statusCode: 404, data: { detail: 'missing' } });
  },
};

async function main() {
  const payload = { company_name: '演示企业', contact_name: '演示联系人', region: '上海' };
  assert.equal(registrationApi.configured(), false);
  await assert.rejects(registrationApi.submit(payload), /未配置/);
  assert.equal(storage.size, 0, 'missing config must not fall back to local registration storage');

  apiBase = 'http://127.0.0.1:8000/';
  storage.set('demo_registration_capability_v1', {});
  const requestsBeforeMalformedCapability = requestLog.length;
  const randomBeforeMalformedCapability = randomCounter;
  await assert.rejects(registrationApi.submit(payload), /凭证结构不可验证/);
  assert.equal(requestLog.length, requestsBeforeMalformedCapability, 'a malformed existing capability must block a new registration');
  assert.equal(randomCounter, randomBeforeMalformedCapability, 'a malformed capability must not allocate a new operation key');
  assert.equal(storage.has('demo_registration_pending_create_v1'), false);
  storage.delete('demo_registration_capability_v1');
  failRequests = true;
  await assert.rejects(registrationApi.submit(payload), /结果未能确认/);
  assert.equal(registrationApi.readCapability(), null, 'offline API must not claim a local registration');
  assert.ok(storage.has('demo_registration_pending_create_v1'), 'retry key must survive the uncertain response');
  const validPendingCreate = storage.get('demo_registration_pending_create_v1');
  storage.set('demo_registration_pending_create_v1', { ...validPendingCreate, operationId: '' });
  const requestsBeforeCorruptRetry = requestLog.length;
  await assert.rejects(registrationApi.submit(payload), /上一条登记创建结果尚未确认/);
  assert.equal(requestLog.length, requestsBeforeCorruptRetry, 'a malformed stored operation key must never be sent');
  storage.set('demo_registration_pending_create_v1', validPendingCreate);
  const requestsBeforeUnreadableRetry = requestLog.length;
  storageReadFailureKey = 'demo_registration_pending_create_v1';
  await assert.rejects(registrationApi.submit(payload), /本机记录不可读取/);
  assert.equal(requestLog.length, requestsBeforeUnreadableRetry, 'an unreadable retry key must block a new write');
  assert.equal(registrationApi.readPending().unreadable, true);
  storageReadFailureKey = '';

  failRequests = false;
  const submitted = await registrationApi.submit(payload);
  assert.equal(submitted.status, 'submitted');
  const capability = registrationApi.readCapability();
  assert.equal(capability.registrationId, 'REG-DEMO-1');
  assert.equal(capability.accessToken, 'one-time-token');
  assert.equal(requestLog[0].url, 'http://127.0.0.1:8000/api/v1/customer-service/demo/registrations');
  assert.equal(requestLog[0].headers['Idempotency-Key'], requestLog[1].headers['Idempotency-Key']);
  assert.equal(requestLog.every((entry) => !entry.url.includes('one-time-token')), true);

  serverRegistration = { ...serverRegistration, status: 'needs_info' };
  assert.equal((await registrationApi.refresh()).status, 'needs_info');
  assert.equal((await registrationApi.submit(payload)).status, 'submitted');
  assert.equal(requestLog.some((entry) => entry.method === 'PATCH'), true);
  serverRegistration = { ...serverRegistration, status: 'needs_info' };
  registrationUpdateOperationId = '';
  registrationUpdatePayload = null;
  registrationUpdateCount = 0;
  const updatePayload = { ...payload, notes: '更新资料' };
  loseUpdateResponseOnce = true;
  await assert.rejects(registrationApi.submit(updatePayload), /结果未能确认/);
  const pendingUpdate = storage.get('demo_registration_pending_update_v1');
  assert.ok(pendingUpdate && pendingUpdate.operationId);
  const patchRequestCount = requestLog.filter((entry) => entry.method === 'PATCH').length;
  const updatedAfterRetry = await registrationApi.submit(updatePayload);
  assert.equal(updatedAfterRetry.status, 'submitted');
  const patchRequests = requestLog.filter((entry) => entry.method === 'PATCH').slice(-2);
  assert.equal(patchRequests.length, 2);
  assert.equal(patchRequests[0].headers['Idempotency-Key'], patchRequests[1].headers['Idempotency-Key']);
  assert.equal(registrationUpdateCount, 1, 'uncertain update retry must not apply twice');
  assert.equal(requestLog.filter((entry) => entry.method === 'PATCH').length, patchRequestCount + 1);
  assert.equal(storage.has('demo_registration_pending_update_v1'), false);
  const currentCapability = registrationApi.readCapability();
  const requestsBeforeUnreadableUpdate = requestLog.length;
  const pendingUpdatePayload = { ...payload, notes: '无法读取待办' };
  storage.set('demo_registration_pending_update_v1', {
    registrationId: currentCapability.registrationId,
    fingerprint: JSON.stringify(pendingUpdatePayload), payload: pendingUpdatePayload,
    operationId: 'registration-update-unreadable-operation-001',
  });
  storageReadFailureKey = 'demo_registration_pending_update_v1';
  await assert.rejects(registrationApi.submit(pendingUpdatePayload), /本机记录不可读取/);
  assert.equal(requestLog.length, requestsBeforeUnreadableUpdate, 'an unreadable update key must block before refresh or write');
  storageReadFailureKey = '';
  const requestCountBeforeAccountSwitch = requestLog.length;
  storage.set('demo_registration_capability_v1', { ...currentCapability, registrationId: 'REG-OTHER' });
  const oldAccountPayload = { company_name: '旧企业', contact_name: '旧联系人', region: '旧地区', notes: '' };
  storage.set('demo_registration_pending_update_v1', {
    registrationId: currentCapability.registrationId,
    fingerprint: JSON.stringify(oldAccountPayload),
    payload: oldAccountPayload,
    operationId: 'registration-update-other-account-operation-01',
  });
  assert.deepEqual(registrationApi.readPending(), { payload: null, blocked: true, action: 'update' });
  await assert.rejects(registrationApi.submit(payload), /上次登记资料更新结果尚未确认/);
  assert.equal(requestLog.length, requestCountBeforeAccountSwitch, 'another account must be blocked before any request');
  storage.delete('demo_registration_pending_update_v1');
  storage.set('demo_registration_capability_v1', currentCapability);
  assert.equal(registrationApi.forgetCapability(), true);
  assert.equal(registrationApi.readCapability(), null);

  storage.clear();
  requestLog.length = 0;
  serverRegistration = null;
  registrationOperationId = '';
  registrationCreateCount = 0;
  loseCreateResponseOnce = true;
  const lostResponsePayload = { company_name: '响应丢失企业', contact_name: '联系人', region: '北京' };
  await assert.rejects(registrationApi.submit(lostResponsePayload), /创建结果未能确认/);
  const pending = storage.get('demo_registration_pending_create_v1');
  assert.ok(pending && pending.operationId);
  const recovered = await registrationApi.submit(lostResponsePayload);
  assert.equal(recovered.status, 'submitted');
  assert.equal(registrationCreateCount, 1, 'retry must not create a second registration');
  assert.equal(storage.has('demo_registration_pending_create_v1'), false);
  assert.equal(registrationApi.forgetPending(), true);
  assert.equal(registrationApi.readPending().needsCheck, true, 'cleanup confirmation marker must survive a fresh page read');
  const requestsBeforeCleanupGate = requestLog.length;
  const randomBeforeCleanupGate = randomCounter;
  await assert.rejects(registrationApi.submit({ ...lostResponsePayload, notes: '新的资料' }), /核对服务端登记状态/);
  assert.equal(requestLog.length, requestsBeforeCleanupGate, 'new registration must be blocked while cleanup confirmation is pending');
  assert.equal(randomCounter, randomBeforeCleanupGate, 'cleanup gate must block operation id generation');
  assert.equal(registrationApi.acknowledgeCleanupCheck(), true);
  console.log('registration API client checks passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
