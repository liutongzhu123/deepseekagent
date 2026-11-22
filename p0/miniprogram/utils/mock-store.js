const QUOTES_KEY = 'demo_quotes_v1';
const MESSAGES_KEY = 'demo_messages_v1';

// Failure cases covered in tests/mock-store.test.js:
// F1: storage read throws or contains a non-array value -> return a safe empty/default list.
// F2: storage write throws (for example, quota reached) -> return failure; never claim saved.
// F3: clearing storage throws -> return failure and let the page preserve its visible state.

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

function addQuote(input) {
  const quotes = listQuotes();
  const quote = {
    id: `DEMO-${Date.now()}`,
    project: input.project,
    standard: input.standard,
    quantity: input.quantity,
    urgency: input.urgency,
    details: input.details,
    status: '待业务人员确认',
    createdAt: new Date().toLocaleString('zh-CN', { hour12: false }),
  };
  quotes.unshift(quote);
  return write(QUOTES_KEY, quotes) ? quote : null;
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

function clearDemoData() {
  try {
    wx.removeStorageSync(QUOTES_KEY);
    wx.removeStorageSync(MESSAGES_KEY);
    return true;
  } catch (error) {
    return false;
  }
}

module.exports = { listQuotes, addQuote, listMessages, addMessage, clearDemoData };
