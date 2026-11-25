const COLLECTION = '/api/v1/customer-service/demo/orders';
const PENDING_KEY = 'demo_order_pending_submit_v1';
const PENDING_UPDATE_KEY = 'demo_order_pending_update_v1';
const CLEANUP_CHECK_KEY = 'demo_order_cleanup_check_v1';
const DEFINITIVE_REJECTIONS = new Set([400, 401, 403, 404, 409, 422]);
const createOperationId = require('./operation-id');
const storageRead = require('./storage-read');

function apiBase() {
  try {
    const app = getApp();
    return String((app.globalData || {}).customerServiceDemoApiBase || '').trim().replace(/\/$/, '');
  } catch (error) { return ''; }
}

function registrationCapability() {
  try {
    const value = wx.getStorageSync('demo_registration_capability_v1');
    if (!value || !value.registrationId || !value.accessToken) return null;
    return value;
  } catch (error) { return null; }
}

function configured() { return Boolean(apiBase()); }

function call(method, path, body, capability, operationId = '') {
  if (!apiBase()) return Promise.reject(new Error('未配置本地委托演示 API 地址'));
  if (!capability) return Promise.reject(new Error('请先完成本地演示登记'));
  return new Promise((resolve, reject) => wx.request({
    url: `${apiBase()}${path}`, method, data: body, timeout: 10000,
    header: { 'X-Demo-Access-Token': capability.accessToken, ...(operationId ? { 'Idempotency-Key': operationId } : {}) },
    success(response) {
      if (response.statusCode >= 200 && response.statusCode < 300) { resolve(response.data); return; }
      const error = new Error((response.data && response.data.detail) || `委托演示服务返回 ${response.statusCode}`);
      error.statusCode = response.statusCode;
      reject(error);
    },
    fail() { reject(new Error('委托演示服务连接中断，响应结果未能确认；请刷新委托记录后再操作')); },
  }));
}

async function submit(payload) {
  if (!apiBase()) throw new Error('未配置本地委托演示 API 地址');
  if (storageRead.requireRead(CLEANUP_CHECK_KEY, '委托状态核对标记')) throw new Error('本机委托待办已清理；请先核对服务端委托状态并确认继续');
  const capability = registrationCapability();
  if (!capability) throw new Error('请先完成本地演示登记');
  const normalized = {
    template_id: String(payload.template_id || '').trim(),
    project_name: String(payload.project_name || '').trim(),
    standard: String(payload.standard || '').trim(),
    sample_name: String(payload.sample_name || '').trim(),
    quantity: String(payload.quantity || '').trim(),
    notes: String(payload.notes || '').trim(),
  };
  const payloadFingerprint = JSON.stringify(normalized);
  let pending = storageRead.requireRead(PENDING_KEY, '委托创建待办');
  let pendingUpdate = storageRead.requireRead(PENDING_UPDATE_KEY, '委托补件待办');
  if (pendingUpdate) throw new Error('上一条补件重提结果尚未确认；请先打开原委托恢复处理');
  if (pending && pending.registrationId !== capability.registrationId) {
    throw new Error('设备上有另一登记账号未确认的委托提交；请先恢复原账号处理');
  }
  if (pending && pending.payloadFingerprint !== payloadFingerprint) {
    throw new Error('上一条委托提交结果尚未确认；请先按原内容重试，成功后再提交新委托');
  }
  if (pending && (!storageRead.isOperationId(pending.operationId)
      || JSON.stringify(pending.payload) !== payloadFingerprint)) {
    throw new Error('委托待办内容或操作编号不可验证，已停止提交以避免重复委托');
  }
  if (!pending) {
    pending = { registrationId: capability.registrationId, payloadFingerprint, payload: normalized, operationId: await createOperationId('order-mini') };
    try { wx.setStorageSync(PENDING_KEY, pending); }
    catch (error) { throw new Error('无法保存委托重试编号；委托未发送'); }
  }
  let result;
  try {
    result = await call('POST', COLLECTION, {
      ...normalized, registration_id: capability.registrationId,
    }, capability, pending.operationId);
  } catch (error) {
    if (DEFINITIVE_REJECTIONS.has(error.statusCode)) {
      try { wx.removeStorageSync(PENDING_KEY); } catch (removeError) { /* A known rejection remains safe to retry. */ }
    }
    throw error;
  }
  try { wx.removeStorageSync(PENDING_KEY); } catch (error) { /* Same key remains safe to replay. */ }
  return result.order;
}

function readPending(orderId = '') {
  try {
    if (storageRead.requireRead(CLEANUP_CHECK_KEY, '委托状态核对标记')) return { payload: null, blocked: true, needsCheck: true };
    const pendingUpdate = storageRead.requireRead(PENDING_UPDATE_KEY, '委托补件待办');
    if (pendingUpdate) {
      const capability = registrationCapability();
      if (!storageRead.isOperationId(pendingUpdate.operationId) || !pendingUpdate.payloadFingerprint
          || JSON.stringify(pendingUpdate.payload) !== pendingUpdate.payloadFingerprint) {
        return { payload: null, blocked: true, unreadable: true, action: 'update', orderId: pendingUpdate.orderId };
      }
      return capability && pendingUpdate.registrationId === capability.registrationId && pendingUpdate.orderId === orderId
        ? { payload: pendingUpdate.payload || null, blocked: false, action: 'update', orderId: pendingUpdate.orderId }
        : { payload: null, blocked: true, action: 'update', orderId: pendingUpdate.orderId };
    }
    const pending = storageRead.requireRead(PENDING_KEY, '委托创建待办');
    if (!pending) return null;
    if (!storageRead.isOperationId(pending.operationId) || !pending.payloadFingerprint
        || JSON.stringify(pending.payload) !== pending.payloadFingerprint) {
      return { payload: null, blocked: true, unreadable: true };
    }
    const capability = registrationCapability();
    return capability && pending.registrationId === capability.registrationId && !orderId
      ? { payload: pending.payload || null, blocked: false }
      : { payload: null, blocked: true };
  } catch (error) { return { payload: null, blocked: true, unreadable: true }; }
}

async function get(orderId) {
  const capability = registrationCapability();
  if (!capability) throw new Error('请先完成本地演示登记');
  const result = await call(
    'GET', `${COLLECTION}/${encodeURIComponent(orderId)}?registration_id=${encodeURIComponent(capability.registrationId)}`,
    null, capability,
  );
  return result.order;
}

async function update(orderId, payload) {
  const capability = registrationCapability();
  if (!capability) throw new Error('请先完成本地演示登记');
  if (storageRead.requireRead(CLEANUP_CHECK_KEY, '委托状态核对标记')) throw new Error('本机委托待办已清理；请先核对服务端委托状态并确认继续');
  const pendingSubmit = storageRead.requireRead(PENDING_KEY, '委托创建待办');
  if (pendingSubmit) throw new Error('上一条新委托提交结果尚未确认；请先恢复原提交');
  const normalized = {
    template_id: String(payload.template_id || '').trim(),
    project_name: String(payload.project_name || '').trim(),
    standard: String(payload.standard || '').trim(),
    sample_name: String(payload.sample_name || '').trim(),
    quantity: String(payload.quantity || '').trim(),
    notes: String(payload.notes || '').trim(),
  };
  const payloadFingerprint = JSON.stringify(normalized);
  let pending = storageRead.requireRead(PENDING_UPDATE_KEY, '委托补件待办');
  if (pending && (pending.registrationId !== capability.registrationId || pending.orderId !== orderId)) {
    throw new Error('设备上有另一账号或委托尚未确认的补件操作；请先恢复原委托');
  }
  if (pending && pending.payloadFingerprint !== payloadFingerprint) {
    throw new Error('上一条补件重提结果尚未确认；请按原内容重试');
  }
  if (pending && (!storageRead.isOperationId(pending.operationId)
      || JSON.stringify(pending.payload) !== payloadFingerprint)) {
    throw new Error('委托补件待办内容或操作编号不可验证，已停止提交以避免重复操作');
  }
  if (!pending) {
    pending = { registrationId: capability.registrationId, orderId, payloadFingerprint, payload: normalized,
      operationId: await createOperationId('order-update-mini') };
    try { wx.setStorageSync(PENDING_UPDATE_KEY, pending); }
    catch (error) { throw new Error('无法保存补件重试编号；资料未发送'); }
  }
  let result;
  try {
    result = await call(
      'PATCH', `${COLLECTION}/${encodeURIComponent(orderId)}?registration_id=${encodeURIComponent(capability.registrationId)}`,
      pending.payload, capability, pending.operationId,
    );
  } catch (error) {
    if (DEFINITIVE_REJECTIONS.has(error.statusCode)) {
      try { wx.removeStorageSync(PENDING_UPDATE_KEY); } catch (removeError) { /* A known rejection is safe to retry with a new operation. */ }
    }
    throw error;
  }
  try { wx.removeStorageSync(PENDING_UPDATE_KEY); } catch (error) { /* Same key remains safe to replay. */ }
  return result.order;
}

async function confirm(orderId, decision, note = '') {
  if (storageRead.requireRead(CLEANUP_CHECK_KEY, '委托状态核对标记')) throw new Error('本机委托待办已清理；请先核对服务端委托状态并确认继续');
  const capability = registrationCapability();
  if (!capability) throw new Error('请先完成本地演示登记');
  const result = await call(
    'POST', `${COLLECTION}/${encodeURIComponent(orderId)}/confirmation?registration_id=${encodeURIComponent(capability.registrationId)}`,
    { decision, note }, capability,
  );
  return result.order;
}

async function list() {
  const capability = registrationCapability();
  if (!capability) throw new Error('请先完成本地演示登记');
  const result = await call('GET', `${COLLECTION}?registration_id=${encodeURIComponent(capability.registrationId)}`, null, capability);
  return result.orders || [];
}

function forgetPending() {
  try {
    if (!prepareCleanupCheck()) return false;
    wx.removeStorageSync(PENDING_KEY);
    wx.removeStorageSync(PENDING_UPDATE_KEY);
    return true;
  } catch (error) { return false; }
}

function prepareCleanupCheck() {
  try { wx.setStorageSync(CLEANUP_CHECK_KEY, { at: Date.now() }); return true; }
  catch (error) { return false; }
}

function acknowledgeCleanupCheck() {
  try { wx.removeStorageSync(CLEANUP_CHECK_KEY); return true; }
  catch (error) { return false; }
}

module.exports = { configured, submit, get, update, confirm, list, readPending, forgetPending, prepareCleanupCheck, acknowledgeCleanupCheck };
