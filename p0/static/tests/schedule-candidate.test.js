const test = require('node:test');
const assert = require('node:assert/strict');
const { parse, toDraft, createDraftGuard, bindFormReset, MAX_INPUT } = require('../schedule-candidate.js');

const validBlock = [
  '项目编号：DEMO-01',
  '项目名称：合成示例项目',
  '计划开始：2099-02-01',
  '计划结束：2099-02-15',
  '状态原文：待业务确认',
  '备注：演示资料',
].join('\n');

test('extracts only explicit fields and keeps exact source and line range', () => {
  const result = parse(`\n${validBlock}`);
  assert.equal(result.candidates.length, 1);
  const candidate = result.candidates[0];
  assert.deepEqual(candidate.fields, {
    project_id: 'DEMO-01', project_name: '合成示例项目', start_date: '2099-02-01',
    end_date: '2099-02-15', status: '待业务确认', note: '演示资料',
  });
  assert.equal(candidate.line_start, 2);
  assert.equal(candidate.line_end, 7);
  assert.equal(candidate.raw, validBlock);
  assert.equal(candidate.adoptable, true);
});

test('F1: rejects malformed lines, unknown labels, and duplicate canonical fields', () => {
  for (const text of [
    `${validBlock}\n无分隔符`,
    `${validBlock}\n客户联系人：张三`,
    `${validBlock}\n项目：另一个项目`,
  ]) {
    const candidate = parse(text).candidates[0];
    assert.equal(candidate.adoptable, false);
    assert.ok(candidate.errors.length);
  }
});

test('F2: does not produce an adoptable candidate without project name', () => {
  const result = parse('项目编号：D-1\n状态原文：待确认').candidates[0];
  assert.equal(result.adoptable, false);
  assert.ok(result.errors.some((error) => error.includes('项目名称')));
});

test('F3: rejects impossible dates and reversed ranges', () => {
  for (const text of [
    validBlock.replace('2099-02-01', '2099-02-30'),
    validBlock.replace('2099-02-15', '2099-01-15'),
  ]) {
    assert.equal(parse(text).candidates[0].adoptable, false);
  }
});

test('F4: separates candidates without leaking values and preserves source line numbers', () => {
  const result = parse(`${validBlock}\n---\n项目名称：第二个项目`);
  assert.equal(result.candidates.length, 2);
  assert.equal(result.candidates[0].fields.project_name, '合成示例项目');
  assert.equal(result.candidates[1].fields.project_id, '');
  assert.equal(result.candidates[1].line_start, 8);
  assert.equal(result.candidates[1].adoptable, true);
});

test('F5: rejects oversized input before parsing', () => {
  const result = parse('x'.repeat(MAX_INPUT + 1));
  assert.equal(result.candidates.length, 0);
  assert.match(result.errors[0], /输入超过/);
});

test('F6: prevents adopting source text that cannot fit the audit note limit', () => {
  const text = `${validBlock}\n备注：${'x'.repeat(451)}`;
  const candidate = parse(text).candidates[0];
  assert.equal(candidate.adoptable, false);
  assert.ok(candidate.errors.some((error) => error.includes('原文超过')));
});

test('adopted draft preserves all source newlines and a durable line reference', () => {
  const candidate = parse(validBlock).candidates[0];
  const draft = toDraft(candidate);
  assert.equal(draft.note, `候选原文：\n${validBlock}`);
  assert.equal(draft.source, '本地演示文本第 1-6 行');
  assert.equal(draft.note.length <= 500, true);
  assert.throws(() => toDraft({ ...candidate, adoptable: false }), /只能采用/);
});

test('F7: changing source locks an adopted draft until re-adoption or explicit manualization', () => {
  const guard = createDraftGuard();
  guard.sourceChanged();
  assert.equal(guard.isStale(), false);
  guard.adopt();
  guard.sourceChanged();
  assert.equal(guard.isStale(), true);
  guard.adopt();
  assert.equal(guard.isStale(), false);
  guard.sourceChanged();
  guard.manualize();
  assert.equal(guard.isStale(), false);
  guard.adopt();
  guard.reset();
  assert.equal(guard.isStale(), false);
});

test('form reset clears adopted/stale state before subsequent manual entry', () => {
  const listeners = {};
  const form = { addEventListener(name, callback) { listeners[name] = callback; } };
  const guard = createDraftGuard();
  let cleared = 0;
  bindFormReset(form, guard, () => { cleared += 1; });
  guard.adopt();
  listeners.reset();
  guard.sourceChanged();
  assert.equal(guard.isStale(), false);
  assert.equal(cleared, 1);
});

test('empty input yields no candidates and no guessed fields', () => {
  assert.deepEqual(parse('  \n\n').candidates, []);
});
