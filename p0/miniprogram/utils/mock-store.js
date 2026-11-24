const QUOTES_KEY = 'demo_quotes_v1';
const MESSAGES_KEY = 'demo_messages_v1';
const PROFILE_KEY = 'demo_profile_v1';
const ORDERS_KEY = 'demo_orders_v1';

// Failure cases covered in tests/mock-store.test.js:
// F1: storage read throws or contains a non-array value -> return a safe empty/default list.
// F2: storage write throws (for example, quota reached) -> return failure; never claim saved.
// F3: clearing storage throws -> return failure; callers reload actual storage state.
// F4: order submission without a local customer profile -> reject without writing.
// F5: incomplete customer/order fields -> reject without writing.
// F6: quote request without standard or quantity -> keep it as a follow-up item, not a confirmed quote.
// F7: handoff without a project -> reject; with a project, preserve the quote context locally.

function read(key, fallback) {
  try {
    const value = wx.getStorageSync(key);
    return value || fallback;
  } catch (error) {
    return fallback;
  }
}

function write(key, value) {
  try {
    wx.setStorageSync(key, value);
    return true;
  } catch (error) {
    return false;
  }
}

function readArray(key, fallback) {
  const value = read(key, fallback);
  return Array.isArray(value) ? value : fallback;
}

function listQuotes() {
  return readArray(QUOTES_KEY, []);
}

function missingQuoteFields(input) {
  const missing = [];
  if (!String(input.standard || '').trim()) missing.push('检测标准');
  if (!String(input.quantity || '').trim()) missing.push('样品数量');
  return missing;
}

function quoteContext(input, destination = 'local') {
  const project = String(input.project || '').trim();
  if (!project) return '';
  const missing = missingQuoteFields(input);
  return [
    destination === 'service'
      ? '【报价需求·已提交至本地客服演示队列；不含报价承诺】'
      : '【报价需求上下文·本机演示，未发送】',
    `检测项目：${project}`,
    `检测标准：${String(input.standard || '').trim() || '待补充'}`,
    `样品数量：${String(input.quantity || '').trim() || '待补充'}`,
    `期望时效：${String(input.urgency || '').trim() || '常规'}`,
    `补充说明：${String(input.details || '').trim() || '无'}`,
    missing.length ? `建议补充：${missing.join('、')}` : '信息已齐全，等待业务人员确认（演示）',
  ].join('\n');
}

function addQuote(input) {
  const missingFields = missingQuoteFields(input);
  const quotes = listQuotes();
  const quote = {
    id: `DEMO-${Date.now()}`,
    project: String(input.project || '').trim(),
    standard: String(input.standard || '').trim(),
    quantity: String(input.quantity || '').trim(),
    urgency: input.urgency,
    details: input.details,
    missingFields,
    guidance: missingFields.length ? `请补充：${missingFields.join('、')}。` : '信息已填写齐全，待业务人员确认（演示）。',
    status: missingFields.length ? '待补充信息（演示）' : '待业务人员确认',
    createdAt: new Date().toLocaleString('zh-CN', { hour12: false }),
  };
  quotes.unshift(quote);
  return write(QUOTES_KEY, quotes) ? quote : null;
}

function getProfile() {
  const profile = read(PROFILE_KEY, null);
  if (!profile || typeof profile !== 'object' || Array.isArray(profile)) return null;
  return ['companyName', 'contactName', 'region'].every((key) => String(profile[key] || '').trim())
    ? profile
    : null;
}

function saveProfile(input) {
  const profile = {
    companyName: String(input.companyName || '').trim(),
    contactName: String(input.contactName || '').trim(),
    region: String(input.region || '').trim(),
    status: '本机演示资料 · 未提交审核',
    updatedAt: new Date().toLocaleString('zh-CN', { hour12: false }),
  };
  if (!profile.companyName || !profile.contactName || !profile.region) return null;
  return write(PROFILE_KEY, profile) ? profile : null;
}

function listOrders() {
  return readArray(ORDERS_KEY, []);
}

function addOrder(input) {
  const profile = getProfile();
  const required = ['templateName', 'project', 'sampleName', 'quantity'];
  if (!profile || required.some((key) => !String(input[key] || '').trim())) return null;
  const orders = listOrders();
  const order = {
    orderNo: `DEMO-WX-${Date.now()}`,
    companyName: profile.companyName,
    contactName: profile.contactName,
    region: profile.region,
    templateName: String(input.templateName).trim(),
    project: String(input.project).trim(),
    standard: String(input.standard || '').trim(),
    sampleName: String(input.sampleName).trim(),
    quantity: String(input.quantity).trim(),
    notes: String(input.notes || '').trim(),
    status: '待模拟业务审核',
    createdAt: new Date().toLocaleString('zh-CN', { hour12: false }),
  };
  orders.unshift(order);
  return write(ORDERS_KEY, orders) ? order : null;
}

function listMessages() {
  return readArray(MESSAGES_KEY, [
    { id: 'welcome', side: 'agent', text: '您好，这里是客服演示窗口。当前消息仅保存在本机，不会发送给客服人员。' },
  ]);
}

function addMessage(text) {
  const messages = listMessages();
  messages.push({ id: `DEMO-MSG-${Date.now()}`, side: 'user', text });
  return write(MESSAGES_KEY, messages) ? messages : null;
}

function addGuidedExchange(question, answer) {
  const userText = String(question || '').trim();
  const agentText = String(answer || '').trim();
  if (!userText || !agentText) return null;
  const messages = listMessages();
  const timestamp = Date.now();
  messages.push(
    { id: `DEMO-MSG-${timestamp}-${messages.length}`, side: 'user', text: userText },
    { id: `DEMO-MSG-${timestamp}-${messages.length + 1}`, side: 'agent', text: agentText },
  );
  return write(MESSAGES_KEY, messages) ? messages : null;
}

function clearDemoData() {
  let cleared = true;
  for (const key of [QUOTES_KEY, MESSAGES_KEY, PROFILE_KEY, ORDERS_KEY]) {
    try {
      wx.removeStorageSync(key);
    } catch (error) {
      cleared = false;
    }
  }
  return cleared;
}

module.exports = {
  listQuotes, addQuote, missingQuoteFields, quoteContext, getProfile, saveProfile, listOrders, addOrder,
  listMessages, addMessage, addGuidedExchange, clearDemoData,
};
