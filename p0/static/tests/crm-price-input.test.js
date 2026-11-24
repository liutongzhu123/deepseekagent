const test = require('node:test');
const assert = require('node:assert/strict');
const { preserveNumericText } = require('../crm-price-input.js');

test('retains the exact decimal input for server-side validation and storage', () => {
  assert.deepEqual(preserveNumericText('2.5', '9007199254740993', '60'), {
    quantity: '2.5', unit_price: '9007199254740993', discount: '60',
  });
});

test('does not serialize underflowed values as zero', () => {
  assert.equal(preserveNumericText('1', '1e-999', '60').unit_price, '1e-999');
});

test('rejects missing, non-finite, and out-of-range price input', () => {
  for (const values of [
    ['', '1', '60'], ['1', '', '60'], ['1', '1', ''],
    ['1', 'Infinity', '60'], ['0', '1', '60'], ['1', '-1', '60'], ['1', '1', '101'],
  ]) assert.throws(() => preserveNumericText(...values));
});
