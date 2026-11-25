const ACCESS_KEY = 'demo_registration_capability_v1';
const PENDING_KEY = 'demo_registration_pending_create_v1';
const PENDING_UPDATE_KEY = 'demo_registration_pending_update_v1';
const CLEANUP_CHECK_KEY = 'demo_registration_cleanup_check_v1';
const COLLECTION = '/api/v1/customer-service/demo/registrations';
const DEFINITIVE_REJECTIONS = new Set([400, 401, 403, 404, 409, 422]);
const createOperationId = require('./operation-id');
const storageRead = require('./storage-read');

function apiBase() {
  try {
    const app = getApp();
    return String((app.globalData || {}).customerServiceDemoApiBase || '').trim().replace(/\/$/, '');
  } catch (error) {
    return '';
  }
}

function configured() {
  return Boolean(apiBase());
}

function isValidCapability(value) {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value)
    && typeof value.registrationId === 'string' && value.registrationId.trim()
    && typeof value.accessToken === 'string' && value.accessToken.trim());
}

function readCapability() {
  const result = storageRead.read(ACCESS_KEY);
  const value = result.value;
  if (!result.ok || !isValidCapability(value)) return null;
  return value;
}

function writeCapability(value) {
  try {
    wx.setStorageSync(ACCESS_KEY, value);
    return true;
  } catch (error) {
    return false;
  }
}

function call(method, path, body, token, operationId = '') {
  const base = apiBase();
  if (!base) return Promise.reject(new Error('未配置本地注册演示 API 地址'));
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${base}${path}`,
      method,
      data: body,
      timeout: 10000,
      header: {
        ...(token ? { 'X-Demo-Access-Token': token } : {}),
        ...(operationId ? { 'Idempotency-Key': operationId } : {}),
      },
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data);
          return;
        }
        const message = response.data && response.data.detail;
        const error = new Error(message || `注册演示服务返回 ${response.statusCode}`);
        error.statusCode = response.statusCode;
        reject(error);
      },
      fail() {
        const message = method === 'POST' && path === COLLECTION
          ? '注册演示服务连接中断，登记创建结果未能确认；请按原内容重试'
          : '注册演示服务连接中断，当前操作结果未能确认；请刷新状态后重试';
        reject(new Error(message));
      },
    });
  });
}

async function refresh() {
  const stored = storageRead.requireRead(ACCESS_KEY, '登记凭证');
  const capability = stored && typeof stored === 'object' && stored.registrationId && stored.accessToken ? stored : null;
  if (!capability) return null;
  const result = await call(
    'GET', `${COLLECTION}/${encodeURIComponent(capability.registrationId)}`, null, capability.accessToken,
  );
  const updated = { ...capability, registration: result.registration };
  if (!writeCapability(updated)) throw new Error('状态读取成功，但本机无法保存凭证状态');
  return result.registration;
}

async function submit(payload) {
  if (!configured()) throw new Error('未配置本地注册演示 API 地址；资料未发送');
  if (storageRead.requireRead(CLEANUP_CHECK_KEY, '登记状态核对标记')) {
    throw new Error('本机登记待办已清理；请先核对服务端登记状态并确认继续');
  }
  const normalized = {
    company_name: String(payload.company_name || '').trim(),
    contact_name: String(payload.contact_name || '').trim(),
    region: String(payload.region || '').trim(),
    notes: String(payload.notes || '').trim(),
  };
  const storedCapability = storageRead.requireRead(ACCESS_KEY, '登记凭证');
  if (storedCapability !== null && !isValidCapability(storedCapability)) {
    throw new Error('本机登记凭证结构不可验证，已停止创建新登记以避免重复记录；请先清理本机登记状态并核对服务端');
  }
  let capability = isValidCapability(storedCapability) ? storedCapability : null;
  if (!capability) {
    if (storageRead.requireRead(PENDING_UPDATE_KEY, '登记更新待办')) throw new Error('设备上有未确认的登记资料更新；请先恢复原登记账号');
    const fingerprint = JSON.stringify(normalized);
    let pending = storageRead.requireRead(PENDING_KEY, '登记创建待办');
    if (pending && (pending.fingerprint !== fingerprint || JSON.stringify(pending.payload) !== fingerprint
        || !storageRead.isOperationId(pending.operationId))) {
      throw new Error('上一条登记创建结果尚未确认，请先按原内容重试');
    }
    if (!pending) {
      pending = { fingerprint, payload: normalized, operationId: await createOperationId('registration-mini') };
      if (!writePendingCreate(pending)) throw new Error('无法保存登记重试编号；资料未发送');
    }
    let created;
    try { created = await call('POST', COLLECTION, pending.payload, '', pending.operationId); }
    catch (error) {
      if (DEFINITIVE_REJECTIONS.has(error.statusCode)) clearPendingCreate();
      throw error;
    }
    capability = {
      registrationId: created.registration.registration_id,
      accessToken: created.access_token,
      registration: created.registration,
    };
    if (!writeCapability(capability)) {
      throw new Error('演示登记草稿已创建，但本机无法安全保存凭证；请联系管理员清理该草稿后重试');
    }
    clearPendingCreate();
  } else {
    const fingerprint = JSON.stringify(normalized);
    let pendingUpdate = storageRead.requireRead(PENDING_UPDATE_KEY, '登记更新待办');
    if (pendingUpdate && (pendingUpdate.registrationId !== capability.registrationId
        || pendingUpdate.fingerprint !== fingerprint || JSON.stringify(pendingUpdate.payload) !== fingerprint
        || !storageRead.isOperationId(pendingUpdate.operationId))) {
      throw new Error('上次登记资料更新结果尚未确认，请先按原内容重试');
    }
    const current = await refresh();
    if (!pendingUpdate && current && ['approved', 'rejected'].includes(current.status)) {
      throw new Error('当前登记已结束，不能重复提交');
    }
    if (!pendingUpdate && current && current.status === 'submitted') return current;
    if (!pendingUpdate) {
      pendingUpdate = {
        registrationId: capability.registrationId,
        fingerprint,
        payload: normalized,
        operationId: await createOperationId('registration-update-mini'),
      };
      if (!writePendingUpdate(pendingUpdate)) throw new Error('无法保存登记更新重试编号；资料未发送');
    }
    let updated;
    try {
      updated = await call(
        'PATCH', `${COLLECTION}/${encodeURIComponent(capability.registrationId)}`,
        pendingUpdate.payload, capability.accessToken, pendingUpdate.operationId,
      );
    } catch (error) {
      if (DEFINITIVE_REJECTIONS.has(error.statusCode)) clearPendingUpdate();
      throw error;
    }
    capability.registration = updated.registration;
    if (!writeCapability(capability)) throw new Error('本机无法保存演示登记凭证');
    clearPendingUpdate();
    if (updated.registration && ['approved', 'rejected'].includes(updated.registration.status)) {
      throw new Error('当前登记已结束，不能重复提交');
    }
    if (updated.registration && updated.registration.status === 'submitted') return updated.registration;
  }
  const result = await call(
    'POST', `${COLLECTION}/${encodeURIComponent(capability.registrationId)}/submit`,
    null, capability.accessToken,
  );
  capability.registration = result.registration;
  if (!writeCapability(capability)) throw new Error('提交结果已收到，但本机无法保存凭证状态');
  return result.registration;
}

function readPendingCreate() {
  const result = storageRead.read(PENDING_KEY);
  if (!result.ok) return { payload: null, blocked: true, unreadable: true, action: 'create' };
  const pending = result.value;
  if (pending && (!storageRead.isOperationId(pending.operationId) || !pending.fingerprint
      || JSON.stringify(pending.payload) !== pending.fingerprint)) {
    return { payload: null, blocked: true, unreadable: true, action: 'create' };
  }
  return pending;
}

function readPendingUpdate() {
  const result = storageRead.read(PENDING_UPDATE_KEY);
  if (!result.ok) return { payload: null, blocked: true, unreadable: true, action: 'update' };
  const pending = result.value;
  if (pending && (!storageRead.isOperationId(pending.operationId) || !pending.registrationId || !pending.fingerprint
      || JSON.stringify(pending.payload) !== pending.fingerprint)) {
    return { payload: null, blocked: true, unreadable: true, action: 'update' };
  }
  return pending;
}

function readPending() {
  const cleanupCheck = storageRead.read(CLEANUP_CHECK_KEY);
  if (!cleanupCheck.ok) return { payload: null, blocked: true, unreadable: true, action: 'cleanup-check' };
  if (cleanupCheck.value) return { payload: null, blocked: true, needsCheck: true, action: 'cleanup-check' };
  const capabilityStorage = storageRead.read(ACCESS_KEY);
  if (!capabilityStorage.ok || (capabilityStorage.present && !isValidCapability(capabilityStorage.value))) {
    return { payload: null, blocked: true, unreadable: true, action: 'capability' };
  }
  const capability = readCapability();
  const update = readPendingUpdate();
  if (update && update.unreadable) return update;
  if (update) {
    return capability && update.registrationId === capability.registrationId
      ? update
      : { payload: null, blocked: true, action: 'update' };
  }
  const create = readPendingCreate();
  if (create && create.unreadable) return create;
  if (create && capability) {
    if (clearPendingCreate()) return null;
    return { payload: null, blocked: true, unreadable: true, action: 'create-cleanup' };
  }
  if (create) return create;
  return null;
}

function writePendingCreate(value) {
  try { wx.setStorageSync(PENDING_KEY, value); return true; } catch (error) { return false; }
}

function clearPendingCreate() {
  try { wx.removeStorageSync(PENDING_KEY); return true; } catch (error) { return false; }
}

function writePendingUpdate(value) {
  try { wx.setStorageSync(PENDING_UPDATE_KEY, value); return true; } catch (error) { return false; }
}

function clearPendingUpdate() {
  try { wx.removeStorageSync(PENDING_UPDATE_KEY); return true; } catch (error) { return false; }
}

function forgetCapability() {
  try {
    wx.removeStorageSync(ACCESS_KEY);
    return true;
  } catch (error) {
    return false;
  }
}

function forgetPending() {
  if (!prepareCleanupCheck()) return false;
  const createCleared = clearPendingCreate();
  const updateCleared = clearPendingUpdate();
  return createCleared && updateCleared;
}

function prepareCleanupCheck() {
  try { wx.setStorageSync(CLEANUP_CHECK_KEY, { at: Date.now() }); return true; }
  catch (error) { return false; }
}

function acknowledgeCleanupCheck() {
  try { wx.removeStorageSync(CLEANUP_CHECK_KEY); return true; }
  catch (error) { return false; }
}

module.exports = { configured, readCapability, readPending, refresh, submit, forgetCapability, forgetPending, prepareCleanupCheck, acknowledgeCleanupCheck };
