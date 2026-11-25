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
  assert.equal(store.saveProfile({ companyName: '演示公司', contactName: '演示联系人', region: '演示地区' }), null);
});

test('F3: clear errors are reported instead of pretending data was removed', () => {
  useStorage();
  global.wx.removeStorageSync = () => { throw new Error('storage unavailable'); };
  assert.equal(store.clearDemoData(), false);
});

test('saved quote and message remain local and can be cleared', () => {
  const values = useStorage();
  const quote = store.addQuote({ project: '虚构检测项目', standard: '演示标准 1', quantity: '2件', urgency: '常规' });
  const messages = store.addMessage('本地客服演示');
  store.saveProfile({ companyName: '演示公司', contactName: '演示联系人', region: '演示地区' });
  const order = store.addOrder({ templateName: '演示委托模板 A', project: '虚构检测项目', sampleName: '虚构样品', quantity: '2件' });
  assert.match(quote.id, /^DEMO-/);
  assert.equal(quote.status, '待业务人员确认');
  assert.equal(messages.at(-1).text, '本地客服演示');
  assert.equal(order.companyName, '演示公司');
  assert.equal(order.contactName, '演示联系人');
  assert.equal(Object.keys(values).sort().join(','), 'demo_messages_v1,demo_orders_v1,demo_profile_v1,demo_quotes_v1');
  assert.equal(store.clearDemoData(), true);
  assert.deepEqual(store.listQuotes(), []);
  assert.deepEqual(store.listOrders(), []);
  assert.equal(store.getProfile(), null);
  assert.equal(store.listMessages().length, 1);
});

test('F4/F5: registration and order validation blocks incomplete or unregistered requests', () => {
  useStorage();
  const validOrder = { templateName: '演示模板 A', project: '虚构项目', sampleName: '虚构样品', quantity: '1件' };
  assert.equal(store.addOrder(validOrder), null);
  assert.equal(store.saveProfile({ companyName: '演示公司', contactName: '', region: '演示地区' }), null);
  assert.equal(store.addOrder(validOrder), null);
  store.saveProfile({ companyName: '演示公司', contactName: '演示联系人', region: '演示地区' });
  assert.equal(store.addOrder({ ...validOrder, quantity: '  ' }), null);
  assert.deepEqual(store.listOrders(), []);
});

test('registered customer details are copied into a confirmed local demo order', () => {
  useStorage();
  store.saveProfile({ companyName: '虚构零部件公司', contactName: '演示联系人', region: '演示地区' });
  const order = store.addOrder({
    templateName: '演示模板 B', project: '气味测试', standard: '演示标准 1',
    sampleName: '虚构内饰片', quantity: '3件', notes: '仅演示',
  });
  assert.equal(order.companyName, '虚构零部件公司');
  assert.equal(order.region, '演示地区');
  assert.equal(order.status, '待模拟业务审核');
  assert.equal(store.listOrders().length, 1);
});

test('F6: incomplete quote records follow-up fields and complete quote waits for human review', () => {
  useStorage();
  const incomplete = store.addQuote({ project: '气味测试', standard: ' ', quantity: '' });
  assert.deepEqual(incomplete.missingFields, ['检测标准', '样品数量']);
  assert.equal(incomplete.status, '待补充信息（演示）');
  assert.match(incomplete.guidance, /检测标准、样品数量/);

  const complete = store.addQuote({ project: '气味测试', standard: '演示标准 1', quantity: '2件' });
  assert.deepEqual(complete.missingFields, []);
  assert.equal(complete.status, '待业务人员确认');
});

test('F7: quote handoff requires a project and retains context in the local service thread', () => {
  useStorage();
  assert.equal(store.quoteContext({ standard: '标准 1' }), '');
  const context = store.quoteContext({ project: '气味测试', standard: '', quantity: '2件', urgency: '加急咨询', details: '虚构内饰片' });
  assert.match(context, /本机演示，未发送/);
  assert.match(context, /检测标准：待补充/);
  assert.match(context, /样品数量：2件/);
  assert.match(context, /建议补充：检测标准/);
  const serviceContext = store.quoteContext({ project: '气味测试', standard: '标准1', quantity: '2件' }, 'service');
  assert.match(serviceContext, /已提交至本地客服演示队列/);
  assert.doesNotMatch(serviceContext, /本机演示，未发送/);
  const messages = store.addMessage(context);
  assert.equal(messages.at(-1).text, context);
});
