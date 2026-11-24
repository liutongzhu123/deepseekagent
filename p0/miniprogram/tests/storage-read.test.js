const test = require('node:test');
const assert = require('node:assert/strict');
const storageRead = require('../utils/storage-read');

test('distinguishes an absent key from falsey or malformed stored values', () => {
  const values = new Map();
  global.wx = {
    getStorageInfoSync() { return { keys: [...values.keys()] }; },
    getStorageSync(key) { return values.get(key); },
  };

  assert.deepEqual(storageRead.read('pending'), { ok: true, present: false, value: null });
  for (const corrupt of ['', 0, false, null]) {
    values.set('pending', corrupt);
    assert.equal(storageRead.read('pending').ok, false, `stored ${String(corrupt)} must not look absent`);
    assert.throws(() => storageRead.requireRead('pending', '待办'), /格式无效/);
  }
});

test('fails closed when storage inventory cannot prove whether a key exists', () => {
  global.wx = { getStorageInfoSync() { throw new Error('storage unavailable'); } };
  assert.equal(storageRead.read('pending').ok, false);
  assert.throws(() => storageRead.requireRead('pending', '待办'), /不可读取/);
});
