(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ScheduleCandidate = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  const MAX_INPUT = 12000;
  const MAX_CANDIDATE = 450;
  const FIELD_NAMES = {
    '项目编号': 'project_id', '项目ID': 'project_id', '编号': 'project_id',
    '项目名称': 'project_name', '项目': 'project_name',
    '计划开始': 'start_date', '开始日期': 'start_date',
    '计划结束': 'end_date', '结束日期': 'end_date',
    '状态': 'status', '状态原文': 'status', '备注': 'note',
  };

  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const [year, month, day] = value.split('-').map(Number);
    const date = new Date(Date.UTC(year, month - 1, day));
    return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  }

  // Failure cases guarded by schedule-candidate.test.js:
  // F1 unknown, malformed, or duplicate fields; F2 missing project name;
  // F3 invalid or reversed dates; F4 block cross-contamination; F5 oversized input;
  // F6 source text too long to preserve with the adopted draft;
  // F7 changing source text locks an adopted draft until re-adoption/manualization.
  function parse(text) {
    const source = String(text ?? '');
    if (source.length > MAX_INPUT) {
      return { candidates: [], errors: [`输入超过 ${MAX_INPUT} 个字符，未解析。`] };
    }

    const candidates = [];
    const blocks = [];
    let current = [];
    for (const [index, line] of source.split(/\r?\n/).entries()) {
      if (!line.trim() || /^\s*---+\s*$/.test(line)) {
        if (current.length) blocks.push(current);
        current = [];
      } else {
        current.push({ line, number: index + 1 });
      }
    }
    if (current.length) blocks.push(current);

    for (const lines of blocks) {
      const fields = { project_id: '', project_name: '', start_date: '', end_date: '', status: '', note: '' };
      const seen = new Set();
      const errors = [];
      for (const { line, number } of lines) {
        const match = line.match(/^\s*([^：:]+?)\s*[：:]\s*(.*?)\s*$/);
        if (!match) {
          errors.push(`第 ${number} 行不是“字段：值”格式`);
          continue;
        }
        const key = match[1].trim();
        const field = FIELD_NAMES[key];
        if (!field) {
          errors.push(`第 ${number} 行字段“${key}”不在演示解析范围内`);
          continue;
        }
        if (seen.has(field)) {
          errors.push(`第 ${number} 行重复填写“${key}”`);
          continue;
        }
        seen.add(field);
        fields[field] = match[2].trim();
      }
      if (!fields.project_name) errors.push('缺少必填字段“项目名称”');
      for (const field of ['start_date', 'end_date']) {
        if (fields[field] && !validDate(fields[field])) errors.push(`“${field === 'start_date' ? '计划开始' : '计划结束'}”必须是有效的 YYYY-MM-DD 日期`);
      }
      if (fields.start_date && fields.end_date && fields.start_date > fields.end_date) {
        errors.push('计划结束不能早于计划开始');
      }
      const raw = lines.map(({ line }) => line).join('\n');
      if (raw.length > MAX_CANDIDATE) errors.push(`原文超过 ${MAX_CANDIDATE} 个字符，无法完整写入备注`);
      candidates.push({
        fields,
        raw,
        line_start: lines[0].number,
        line_end: lines[lines.length - 1].number,
        errors,
        adoptable: errors.length === 0,
      });
    }
    return { candidates, errors: [] };
  }

  function toDraft(candidate) {
    if (!candidate || !candidate.adoptable) throw new Error('只能采用通过校验的排期候选');
    const fields = candidate.fields;
    return {
      ...fields,
      source: `本地演示文本第 ${candidate.line_start}-${candidate.line_end} 行`,
      note: `候选原文：\n${candidate.raw}`,
    };
  }

  function createDraftGuard() {
    let adopted = false;
    let stale = false;
    return {
      adopt() { adopted = true; stale = false; },
      sourceChanged() { if (adopted) stale = true; },
      manualize() { adopted = false; stale = false; },
      reset() { adopted = false; stale = false; },
      isStale() { return stale; },
    };
  }

  function bindFormReset(form, guard, onReset) {
    if (!form || typeof form.addEventListener !== 'function') throw new Error('排期表单不支持 reset 事件');
    form.addEventListener('reset', () => {
      guard.reset();
      if (typeof onReset === 'function') onReset();
    });
  }

  return { parse, toDraft, createDraftGuard, bindFormReset, MAX_INPUT, MAX_CANDIDATE };
});
