const test = require('node:test');
const assert = require('node:assert/strict');
const store = require('../utils/mock-store');

function useStorage(initial = {}) {
  const values = { ...initial };
  global.wx = {
    getStorageSync: (key) => values[key],
    setStorageSync: (key, value) => { values[key] = value; },
    removeStorageSync: (key) => { delete values[key]; },
  };
  return values;
}

test('F1: storage read errors and malformed list values fall back safely', () => {
  global.wx = { getStorageSync() { throw new Error('storage unavailable'); } };
  assert.deepEqual(store.listQuotes(), []);
  assert.equal(store.listMessages().length, 1);

  useStorage({ demo_quotes_v1: { broken: true }, demo_messages_v1: 'broken' });
  assert.deepEqual(store.listQuotes(), []);
  assert.equal(store.listMessages().length, 1);
});

test('F2: a storage write error is reported as failure', () => {
  useStorage();
  global.wx.setStorageSync = () => { throw new Error('storage full'); };
  assert.equal(store.addQuote({ project: '虚构检测项目' }), null);
  assert.equal(store.addMessage('本地演示消息'), null);
});

test('F3: clear errors are reported instead of pretending data was removed', () => {
  useStorage();
  global.wx.removeStorageSync = () => { throw new Error('storage unavailable'); };
  assert.equal(store.clearDemoData(), false);
});

test('saved quote and message remain local and can be cleared', () => {
  const values = useStorage();
  const quote = store.addQuote({ project: '虚构检测项目', urgency: '常规' });
  const messages = store.addMessage('本地客服演示');
  assert.match(quote.id, /^DEMO-/);
  assert.equal(quote.status, '待业务人员确认');
  assert.equal(messages.at(-1).text, '本地客服演示');
  assert.equal(Object.keys(values).sort().join(','), 'demo_messages_v1,demo_quotes_v1');
  assert.equal(store.clearDemoData(), true);
  assert.deepEqual(store.listQuotes(), []);
  assert.equal(store.listMessages().length, 1);
});
