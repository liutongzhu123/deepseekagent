const test = require('node:test');
const assert = require('node:assert/strict');
const createSecureOperationId = require('../operation-id.js');

test('creates a stable-format operation id from cryptographic bytes', () => {
  let received;
  const id = createSecureOperationId('quote', {
    getRandomValues(bytes) {
      received = bytes;
      bytes.fill(0xab);
      return bytes;
    },
  });
  assert.ok(received instanceof Uint8Array);
  assert.equal(received.length, 16);
  assert.equal(id, `quote-${'ab'.repeat(16)}`);
});

test('fails closed when cryptographic randomness is unavailable', () => {
  assert.throws(() => createSecureOperationId('reply', null), /安全重试编号/);
  assert.throws(() => createSecureOperationId('reply', {}), /安全重试编号/);
});
