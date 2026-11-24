const COLLECTION = '/api/v1/customer-service/demo/conversations';
const LAST_KEY = 'demo_service_conversation_v1';
const PENDING_KEY = 'demo_service_pending_send_v1';
const CLEANUP_CHECK_KEY = 'demo_service_cleanup_check_v1';
const DEFINITIVE_REJECTIONS = new Set([400, 401, 403, 404, 409, 422]);
const createOperationId = require('./operation-id');
const storageRead = require('./storage-read');
function apiBase() {
  try { return String((getApp().globalData || {}).customerServiceDemoApiBase || '').trim().replace(/\/$/, ''); }
  catch (error) { return ''; }
}
function capability() {
  try {
    const value = wx.getStorageSync('demo_registration_capability_v1');
    return value && value.registrationId && value.accessToken ? value : null;
  } catch (error) { return null; }
}
function configured() { return Boolean(apiBase()); }
function call(method, path, body, auth, operationId) {
  if (!apiBase()) return Promise.reject(new Error('未配置本地客服演示 API 地址'));
  if (!auth) return Promise.reject(new Error('请先完成本地演示登记'));
  return new Promise((resolve, reject) => wx.request({
    url: `${apiBase()}${path}`, method, data: body, timeout: 10000,
    header: { 'X-Demo-Access-Token': auth.accessToken, 'Idempotency-Key': operationId },
    success(response) {
      if (response.statusCode >= 200 && response.statusCode < 300) { resolve(response.data); return; }
      const error = new Error((response.data && response.data.detail) || `客服演示服务返回 ${response.statusCode}`);
      error.statusCode = response.statusCode;
      reject(error);
    },
    fail() { reject(new Error('客服演示服务连接中断，响应结果未能确认；请刷新会话记录后重试操作')); },
  }));
}
async function list() {
  if (!apiBase()) throw new Error('未配置本地客服演示 API 地址');
  const auth = capability();
  if (!auth) throw new Error('请先完成本地演示登记');
  const result = await call('GET', `${COLLECTION}?registration_id=${encodeURIComponent(auth.registrationId)}`, null, auth);
  return result.conversations || [];
}
async function get(conversationId) {
  if (!apiBase()) throw new Error('未配置本地客服演示 API 地址');
  const auth = capability();
  if (!auth) throw new Error('请先完成本地演示登记');
  const result = await call('GET', `${COLLECTION}/${encodeURIComponent(conversationId)}?registration_id=${encodeURIComponent(auth.registrationId)}`, null, auth);
  return result.conversation;
}
async function send(text, conversationId = '') {
  if (!apiBase()) throw new Error('未配置本地客服演示 API 地址');
  if (storageRead.requireRead(CLEANUP_CHECK_KEY, '客服状态核对标记')) throw new Error('本机客服待办已清理；请先刷新并核对服务端会话，再确认继续');
  const auth = capability();
  if (!auth) throw new Error('请先完成本地演示登记');
  const normalized = String(text || '').trim();
  if (!normalized) throw new Error('消息不能为空');
  let pending = storageRead.requireRead(PENDING_KEY, '客服消息待办');
  if (pending && pending.registrationId !== auth.registrationId) {
    throw new Error('设备上有另一登记账号未确认的消息；请先清除旧消息，再继续当前账号');
  }
  if (pending && pending.text !== normalized) {
    throw new Error('上一条消息结果尚未确认，请先重试原消息');
  }
  if (pending && (!storageRead.isOperationId(pending.operationId)
      || typeof pending.conversationId !== 'string' || pending.registrationId !== auth.registrationId)) {
    throw new Error('客服消息待办内容或操作编号不可验证，已停止提交以避免重复消息');
  }
  if (!pending) {
    pending = { registrationId: auth.registrationId, text: normalized, conversationId, operationId: await createOperationId('mini') };
    try { wx.setStorageSync(PENDING_KEY, pending); }
    catch (error) { throw new Error('无法保存本次消息的重试编号；消息未发送'); }
  }
  const targetConversationId = pending.conversationId;
  const path = targetConversationId
    ? `${COLLECTION}/${encodeURIComponent(targetConversationId)}/messages?registration_id=${encodeURIComponent(auth.registrationId)}`
    : COLLECTION;
  const body = targetConversationId
    ? { text: normalized }
    : { registration_id: auth.registrationId, text: normalized };
  let result;
  try {
    result = await call('POST', path, body, auth, pending.operationId);
  } catch (error) {
    if (DEFINITIVE_REJECTIONS.has(error.statusCode)) {
      try { wx.removeStorageSync(PENDING_KEY); } catch (removeError) { /* A known rejection is safe to retry with a new operation. */ }
    }
    throw error;
  }
  const conversation = result.conversation;
  if (!writeLast(conversation.conversation_id)) {
    throw new Error('消息已存档，但无法保存会话编号；请重试以安全恢复');
  }
  try { wx.removeStorageSync(PENDING_KEY); }
  catch (error) { /* The same id remains safe to retry if local cleanup fails. */ }
  return conversation;
}
function readLast() {
  try { return String(wx.getStorageSync(LAST_KEY) || ''); } catch (error) { return ''; }
}
function writeLast(id) {
  try { wx.setStorageSync(LAST_KEY, id); return true; } catch (error) { return false; }
}
function forgetLast() {
  try { wx.removeStorageSync(LAST_KEY); return true; } catch (error) { return false; }
}
function readPending() {
  try {
    if (storageRead.requireRead(CLEANUP_CHECK_KEY, '客服状态核对标记')) return { text: '', blocked: true, needsCheck: true };
    const pending = storageRead.requireRead(PENDING_KEY, '客服消息待办');
    if (!pending) return null;
    if (!storageRead.isOperationId(pending.operationId) || typeof pending.text !== 'string'
        || typeof pending.conversationId !== 'string' || !pending.registrationId) {
      return { text: '', blocked: true, unreadable: true };
    }
    const auth = capability();
    return auth && pending.registrationId === auth.registrationId
      ? { text: pending.text, blocked: false }
      : { text: '', blocked: true };
  } catch (error) { return { text: '', blocked: true, unreadable: true }; }
}
function discardPending() {
  try {
    if (!prepareDiscardCheck()) return false;
    wx.removeStorageSync(PENDING_KEY);
    return true;
  } catch (error) { return false; }
}
function prepareDiscardCheck() {
  try { wx.setStorageSync(CLEANUP_CHECK_KEY, { at: Date.now() }); return true; }
  catch (error) { return false; }
}
function acknowledgeCleanupCheck() {
  try { wx.removeStorageSync(CLEANUP_CHECK_KEY); return true; }
  catch (error) { return false; }
}
module.exports = { configured, list, get, send, readLast, writeLast, forgetLast, readPending, discardPending, prepareDiscardCheck, acknowledgeCleanupCheck };
