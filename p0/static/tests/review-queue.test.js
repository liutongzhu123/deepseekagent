const test = require('node:test');
const assert = require('node:assert/strict');
const filterReviewQueueItems = require('../review-queue.js');

test('keeps a review with an uncertain response visible outside the selected status', () => {
  const rows = [
    { id: 'pending', status: 'approved' },
    { id: 'ordinary', status: 'approved' },
    { id: 'actionable', status: 'submitted' },
  ];
  const filtered = filterReviewQueueItems(rows, 'submitted', (item) => item.id === 'pending');
  assert.deepEqual(filtered.map((item) => item.id), ['pending', 'actionable']);
});

test('returns all review rows when no status filter is selected', () => {
  const rows = [{ id: 'one', status: 'approved' }, { id: 'two', status: 'submitted' }];
  assert.equal(filterReviewQueueItems(rows, '', () => false), rows);
});

test('keeps pending actions after timeout, too-early, and rate-limit responses', () => {
  for (const status of [408, 425, 429, 500, undefined]) {
    assert.equal(filterReviewQueueItems.isDefinitiveClientRejection(status), false);
  }
  for (const status of [400, 409, 422]) {
    assert.equal(filterReviewQueueItems.isDefinitiveClientRejection(status), true);
  }
});

test('keeps pending conversation actions visible through every queue filter', () => {
  const rows = [
    { id: 'pending', status: 'archived', company: 'Other', priority: false, unread: 0 },
    { id: 'ordinary', status: 'open', company: 'Acme', priority: true, unread: 1 },
  ];
  const visible = filterReviewQueueItems.filterWithPendingOverride(
    rows, 'open',
    (item) => item.company === 'Acme' && item.priority && item.unread > 1,
    (item) => item.id === 'pending',
  );
  assert.deepEqual(visible.map((item) => item.id), ['pending']);
});
