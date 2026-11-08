'use strict';

const $ = (id) => document.getElementById(id);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const params = new URLSearchParams(location.search);

const ROLE_NAMES = { admin: '管理员', reviewer: '复核人', sales: '销售', viewer: '只读' };
const ROLE_DESCRIPTIONS = {
  admin: '全部权限 · 用户与审计',
  reviewer: '报告审核 · 标准复核 / 发布',
  sales: '报价 · 知识问答',
  viewer: '只读查询 · 知识问答',
};
const FIELD_NAMES = { name: '实检项目', requirement: '技术要求', method: '试验方法' };
const FEEDBACK_ACTIONS = { correct: '修正', confirm: '确认', supplement: '补充' };
const TEMPLATE_NAMES = { 'BYD-odor': '比亚迪气味报告模板', generic: '通用报告模板' };
const SEVERITY_NAMES = { high: '高', medium: '中', low: '低', info: '提示' };
const SEVERITY_TONES = { high: 'danger', medium: 'warn', low: 'neutral', info: 'neutral' };
const LAYER_LABELS = { deterministic: '确定性', crosscheck: '跨文档', partial: '部分规则', dsl: '规则 DSL', semantic: '语义' };
const QUOTE_HEADER_NAMES = { item: '试验项目', price: '单价', quantity: '数量', unit: '单位', sample: '样品名称' };
const PERMISSIONS = {
  'report:audit': ['admin', 'reviewer'],
  'report:read': ['admin', 'reviewer'],
  'standard:parse': ['admin', 'reviewer'],
  'standard:review': ['admin', 'reviewer'],
  'standard:publish': ['admin', 'reviewer'],
  'quote:generate': ['admin', 'sales'],
  'feedback:read': ['admin'],
  'user:manage': ['admin'],
  'audit:read': ['admin'],
  'config:read': ['admin'],
  'config:manage': ['admin'],
  'config:manage:report': ['admin', 'reviewer'],
  'config:manage:standard': ['admin', 'reviewer'],
  'config:manage:quote': ['admin', 'sales'],
  'config:manage:business': ['admin', 'sales'],
};
const CONFIG_SECTION_PERMISSIONS = {
  report: 'config:manage:report',
  standard: 'config:manage:standard',
  quote: 'config:manage:quote',
  business: 'config:manage:business',
};

const EVENT_LABELS = {
  login_success: '登录成功',
  login_failed: '登录失败',
  login_locked: '账号锁定',
  login_throttled: '登录限流',
  logout: '退出登录',
  user_created: '创建用户',
  user_updated: '更新用户',
  user_password_reset: '重置密码',
  password_changed: '修改密码',
  report_audited: '报告审核',
  report_audited_markdown: '导出审核报告',
  records_validated: '记录校验',
  standard_parsed: '解析标准',
  standard_reviewed: '复核标准',
  standard_published: '发布标准',
  standard_unpublished: '下架标准',
  standard_ask_feedback: '问答反馈',
  standard_rolled_back: '回滚标准修订',
  standard_exported: '导出标准',
  standard_project_assigned: '标准项目授权',
  standard_asked: '知识问答',
  quote_generated: '生成报价',
  quote_downloaded: '下载报价',
  audit_exported: '导出审计',
  kb_rebuilt: '重建索引',
  admin_config_updated: '更新规则配置',
  admin_config_reset: '恢复默认规则',
};

const EVENT_OPTIONS = [
  ['login_success', '登录成功'],
  ['login_failed', '登录失败'],
  ['login_locked', '账号锁定'],
  ['login_throttled', '登录限流'],
  ['logout', '退出登录'],
  ['user_created', '创建用户'],
  ['user_updated', '更新用户'],
  ['user_password_reset', '重置密码'],
  ['password_changed', '修改密码'],
  ['report_audited', '报告审核'],
  ['records_validated', '记录校验'],
  ['standard_reviewed', '复核标准'],
  ['standard_published', '发布标准'],
  ['standard_unpublished', '下架标准'],
  ['standard_ask_feedback', '问答反馈'],
  ['standard_rolled_back', '回滚标准修订'],
  ['standard_exported', '导出标准'],
  ['standard_asked', '知识问答'],
  ['quote_generated', '生成报价'],
  ['quote_downloaded', '下载报价'],
  ['audit_exported', '导出审计'],
  ['admin_config_updated', '更新规则配置'],
  ['admin_config_reset', '恢复默认规则'],
];

const state = {
  me: null,
  users: [],
  standards: [],
  current: null,
  versions: [],
  selectedIndex: -1,
  reports: [],
  reportPage: { offset: 0, limit: 20, total: 0, query: '' },
  currentReport: null,
  quotes: [],
  quotesTotal: 0,
  lastAnswer: null,
  feedback: null,
  adminView: 'users',
  userRoleFilter: null,
  projectUser: null,
  resetUser: null,
  kbHealth: null,
  audit: { events: [], total: 0, limit: 50, offset: 0, all: [] },
  config: null,
  moduleConfig: null,
  moduleConfigError: '',
};

const SAMPLE_RECORDS = `{
  "records": [
    {"task_no":"YF25HNE8","report_no":"QA25NY1WHNE81","sample_no":"TS25HNE8B-03","limit":3.5,"scores":[3.5,4.0,4.0],"reported_mean":3.8,"test_date":"2026-01-08","eval_date":"2026-01-08","judgment":"不符合"},
    {"task_no":"YF25HNE8","report_no":"QA25NY2WHNE81","sample_no":"TS25HNE8B-04","limit":3.5,"scores":[3.5,3.5,4.0],"reported_mean":3.7,"test_date":"2026-01-08","eval_date":"2026-01-08","judgment":"不符合"}
  ]
}`;

/* ---------------------------------------------------------------- utilities */
const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function escapeHtml(value) {
  return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}
function ic(name, cls = '') {
  return `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
}
function money(value) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fileSize(size) {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${Math.max(1, Math.round(size))} B`;
}
function initials(name) {
  const text = String(name || '').trim();
  if (!text) return '··';
  return text.length <= 2 ? text : text.slice(0, 2);
}
function formatTime(value, withSeconds = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, '0');
  const base = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return withSeconds ? `${base}:${pad(date.getSeconds())}` : base;
}
function reviewerName() {
  return (state.me && (state.me.display_name || state.me.username)) || '复核人';
}
function kpi(value, label, tone = 'neutral', icon = 'activity', sub = '') {
  return `<div class="kpi tone-${tone}"><div class="kpi-icon">${ic(icon)}</div><div><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span>${sub ? `<div class="kpi-sub">${escapeHtml(sub)}</div>` : ''}</div></div>`;
}
function busy(button, on, label = '处理中…') {
  if (!button) return;
  if (on) {
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="spinner"></span>${escapeHtml(label)}`;
  } else {
    button.disabled = false;
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
    }
  }
}
function toast(message, type = 'success') {
  const stack = $('toastStack');
  const element = document.createElement('div');
  element.className = `toast ${type}`;
  const icon = type === 'error' ? 'alert' : type === 'info' ? 'activity' : 'check';
  element.innerHTML = `${ic(icon)}<span>${escapeHtml(message)}</span>`;
  stack.appendChild(element);
  setTimeout(() => {
    element.style.transition = 'opacity .2s ease, transform .2s ease';
    element.style.opacity = '0';
    element.style.transform = 'translateY(6px)';
    setTimeout(() => element.remove(), 220);
  }, 4200);
}
function showError(error) {
  if (String(error && error.message).includes('登录状态已失效')) return;
  toast((error && error.message) || String(error), 'error');
}
async function jsonFetch(url, options) {
  const response = await fetch(url, options);
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = { detail: String(error) };
  }
  if (response.status === 401) {
    window.location.replace('login');
    throw new Error('登录状态已失效');
  }
  if (!response.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || JSON.stringify(item)).join('；');
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return data;
}
function can(action) {
  return Boolean(state.me && (PERMISSIONS[action] || []).includes(state.me.role));
}
function applyRoleUi() {
  const canAudit = can('report:audit');
  const canParse = can('standard:parse');
  const canReview = can('standard:review');
  const canPublish = can('standard:publish');
  const canQuote = can('quote:generate');
  const canReadReports = can('report:read');
  $('reportUploadCard').classList.toggle('hidden', !canAudit);
  $('reportResultCard').classList.toggle('hidden', !canAudit);
  $('recordsCard').classList.toggle('hidden', !canAudit);
  $('reportHistoryCard').classList.toggle('hidden', !canReadReports);
  $('reportRoleNotice').classList.toggle('hidden', canAudit);
  if (!canAudit) {
    $('reportRoleNotice').innerHTML = `${ic('lock')}<span>当前角色为 ${escapeHtml(ROLE_NAMES[state.me.role] || state.me.role)}，只能查看规则说明；报告审核需要复核人或管理员权限。</span>`;
  }
  $('stdUploadBlock').classList.toggle('hidden', !canParse);
  $('stdRoleNotice').classList.toggle('hidden', canParse);
  if (!canParse) {
    $('stdRoleNotice').innerHTML = `${ic('eye')}<span>当前角色为 ${escapeHtml(ROLE_NAMES[state.me.role] || state.me.role)}，可查看与导出标准；解析、复核和发布需要复核人或管理员权限。</span>`;
  }
  $('publishBtn').classList.toggle('hidden', !canPublish);
  $('unpublishBtn').classList.toggle('hidden', !canPublish);
  ['saveBtn', 'confirmBtn', 'addItemBtn'].forEach((id) => $(id).classList.toggle('hidden', !canReview));
  ['editName', 'editReq', 'editMethod'].forEach((id) => { $(id).disabled = !canReview; });
  $('quoteUploadCard').classList.toggle('hidden', !canQuote);
  $('quoteResultCard').classList.toggle('hidden', !canQuote);
  $('quoteHistoryCard').classList.toggle('hidden', !canQuote);
  $('quoteRoleNotice').classList.toggle('hidden', canQuote);
  if (!canQuote) {
    $('quoteRoleNotice').innerHTML = `${ic('lock')}<span>当前角色为 ${escapeHtml(ROLE_NAMES[state.me.role] || state.me.role)}，没有报价权限；报价需要销售或管理员权限。</span>`;
  }
}

/* ---------------------------------------------------------------- modal */
function openModal(id) {
  const dialog = $(id);
  if (dialog && !dialog.open) dialog.showModal();
}
function closeModal(id) {
  const dialog = $(id);
  if (dialog && dialog.open) dialog.close();
}
let confirmResolver = null;
function settleConfirm(value) {
  const resolver = confirmResolver;
  if (!resolver) return;
  confirmResolver = null;
  const dialog = $('modalConfirm');
  if (dialog.open) dialog.close();
  resolver(value);
}
function confirmDialog({ title, message, confirmText = '确认', tone = 'primary' }) {
  return new Promise((resolve) => {
    confirmResolver = resolve;
    $('confirmTitle').textContent = title;
    $('confirmMessage').textContent = message;
    const ok = $('confirmOk');
    ok.textContent = confirmText;
    ok.className = `btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}`;
    openModal('modalConfirm');
  });
}

/* ---------------------------------------------------------------- dropzone */
function setupDropzone(dzId, inputId, meta) {
  const dz = $(dzId);
  const input = $(inputId);
  if (!dz || !input) return;
  const title = dz.querySelector('.dz-title');
  const sub = dz.querySelector('.dz-sub');
  const update = () => {
    const selected = Array.from(input.files || []);
    dz.classList.toggle('has-file', selected.length > 0);
    if (!selected.length) {
      title.textContent = meta.title;
      sub.textContent = meta.sub;
      return;
    }
    if (selected.length === 1) {
      title.textContent = selected[0].name;
      sub.textContent = `${fileSize(selected[0].size)} · 已选择，点击可重新选择`;
      return;
    }
    const total = selected.reduce((sum, file) => sum + file.size, 0);
    const names = selected.slice(0, 3).map((file) => file.name).join('、');
    title.textContent = `${selected.length} 份文件`;
    sub.textContent = `共 ${fileSize(total)} · ${names}${selected.length > 3 ? ' 等' : ''}，点击可重新选择`;
  };
  dz.addEventListener('click', (event) => {
    if (event.target === input) return;
    input.click();
  });
  input.addEventListener('change', update);
  ['dragenter', 'dragover'].forEach((type) => dz.addEventListener(type, (event) => {
    event.preventDefault();
    dz.classList.add('dragover');
  }));
  ['dragleave', 'dragend'].forEach((type) => dz.addEventListener(type, () => dz.classList.remove('dragover')));
  dz.addEventListener('drop', (event) => {
    event.preventDefault();
    dz.classList.remove('dragover');
    const dropped = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
    if (!dropped.length) return;
    if (!input.multiple) dropped.splice(1);
    const transfer = new DataTransfer();
    dropped.forEach((file) => transfer.items.add(file));
    input.files = transfer.files;
    input.dispatchEvent(new Event('change'));
  });
}

/* ---------------------------------------------------------------- shell */
const TAB_META = {
  report: { title: '报告审核' },
  standards: { title: '标准解析' },
  quote: { title: '报价助手' },
  knowledge: { title: '知识问答' },
  admin: { title: '权限、审计与规则' },
};
function closeSidebar() {
  $('sidebar').classList.remove('open');
  $('navBackdrop').classList.remove('show');
}
function setSignedInUser(user) {
  const name = user.display_name || user.username;
  const role = ROLE_NAMES[user.role] || user.role;
  const department = user.department ? ` · ${escapeHtml(user.department)}` : '';
  $('userBox').innerHTML = `
    <span class="avatar">${escapeHtml(initials(name))}</span>
    <div class="user-meta"><b>${escapeHtml(name)}</b><span>${escapeHtml(role)}${department}</span></div>
    <button class="logout-btn" id="logoutBtn" title="退出登录" aria-label="退出登录">${ic('logout')}</button>`;
  $('logoutBtn').addEventListener('click', async () => {
    try { await jsonFetch('api/v1/auth/logout', { method: 'POST' }); } catch (error) { /* already signed out */ }
    window.location.replace('login');
  });
}
async function loadMe() {
  try {
    const data = await jsonFetch('api/v1/auth/me');
    state.me = data.user;
    setSignedInUser(state.me);
    applyRoleUi();
    if (!can('user:manage')) {
      $('adminPageTitle').textContent = '规则配置';
      $('adminPageDesc').textContent = '维护你所负责业务链路的规则；保存后对新的请求生效，并写入审计日志。';
    }
    $('adminNavSection').classList.toggle('hidden', !canAnyConfig());
    return true;
  } catch (error) {
    $('userBox').innerHTML = `<span class="avatar">!</span><div class="user-meta"><b>未登录</b><span>请重新登录</span></div>`;
    return false;
  }
}
function switchTab(name) {
  if (!TAB_META[name]) name = 'report';
  if (name === 'admin' && !canAnyConfig()) name = 'report';
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.tab === name));
  $$('.panel').forEach((panel) => panel.classList.toggle('hidden', panel.id !== `panel-${name}`));
  $('crumbCurrent').textContent = TAB_META[name].title;
  closeSidebar();
  if (name === 'knowledge') {
    loadKbHealth();
    loadKbStandards();
    if (can('feedback:read')) loadFeedbackStats().catch(() => {});
  }
  if (name === 'report' || name === 'standards' || name === 'quote') loadModuleConfig();
  if (name === 'report' && can('report:read')) {
    loadReportInputs().catch(() => {});
    loadReports().catch(showError);
  }
  if (name === 'quote' && can('quote:generate')) loadQuotes().catch(showError);
  if (name === 'admin') {
    if (can('user:manage')) loadAdmin().catch(showError);
    switchAdminView(state.adminView || (can('user:manage') ? 'users' : 'config'));
  }
  const nextParams = new URLSearchParams(location.search);
  nextParams.set('tab', name);
  if (name !== 'admin') {
    nextParams.delete('view');
    nextParams.delete('section');
  }
  history.replaceState(null, '', `${location.pathname}?${nextParams.toString()}`);
}

/* ---------------------------------------------------------------- module rule config */
function configMetaText(config) {
  const revision = config.revision ?? 0;
  const updated = config.updated_at ? formatTime(config.updated_at, true) : '尚未保存，当前为默认规则';
  const by = config.updated_by ? ` · ${config.updated_by}` : '';
  return `管理端配置 v${revision} · ${updated}${by}`;
}
function configFact(label, value) {
  return `<span class="config-fact"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></span>`;
}
function canConfig(section) {
  return can(CONFIG_SECTION_PERMISSIONS[section] || 'config:manage');
}
function canAnyConfig() {
  return Object.keys(CONFIG_SECTION_PERMISSIONS).some(canConfig);
}
function configAccessText(section) {
  return canConfig(section) ? '' : ' · 由管理员维护';
}
function applyConfigAccessory() {
  $$('.js-open-admin-config').forEach((button) => button.classList.toggle('hidden', !canConfig(button.dataset.configSection)));
}
function aliasRow(label, values) {
  const chips = (values || []).map((value) => `<span class="chip">${escapeHtml(value)}</span>`).join('');
  return `<div class="alias-row"><span class="alias-label">${escapeHtml(label)}</span><div class="alias-values">${chips || '<span class="muted">—</span>'}</div></div>`;
}
function configErrorState(message) {
  $('reportConfigInline').textContent = message;
  $('reportConfigRev').textContent = '—';
  $('reportRules').innerHTML = `<div class="muted">${escapeHtml(message)}</div>`;
  ['std', 'quote'].forEach((prefix) => {
    $(`${prefix}ConfigInline`).textContent = message;
    $(`${prefix}ConfigMeta`).textContent = message;
    $(`${prefix}ConfigFacts`).innerHTML = '';
    $(`${prefix}ConfigRev`).textContent = '—';
  });
  $('stdConfigAliases').innerHTML = '';
  $('stdConfigFields').innerHTML = `<tr><td colspan="3" class="muted">${escapeHtml(message)}</td></tr>`;
  $('quoteConfigHeaders').innerHTML = `<tr><td colspan="2" class="muted">${escapeHtml(message)}</td></tr>`;
  $('quoteConfigUnits').innerHTML = '';
  $('quoteConfigColumns').innerHTML = `<tr><td colspan="3" class="muted">${escapeHtml(message)}</td></tr>`;
}
function renderReportModuleConfig(config) {
  const report = config.report || { rules: [] };
  const rules = report.rules || [];
  const enabled = rules.filter((rule) => rule.enabled !== false).length;
  const severityOf = (rule) => {
    const severity = SEVERITY_NAMES[rule.severity] || rule.severity || '—';
    const tone = SEVERITY_TONES[rule.severity] || 'neutral';
    return { severity, tone };
  };
  $('ruleCount').textContent = enabled;
  $('ruleHeadPill').innerHTML = `${ic('shield')}${enabled} / ${rules.length} 条规则启用`;
  $('reportConfigRev').textContent = `v${config.revision ?? 0}`;
  $('reportConfigInline').textContent = [
    `均值容差 ${report.mean_tolerance ?? 0.05} 分`,
    report.expected_notices ? `注意事项 ${report.expected_notices} 条` : '注意事项不校验',
    `已停用 ${rules.length - enabled} 条`,
  ].join(' · ') + configAccessText('report');
  $('reportRules').innerHTML = rules.map((rule) => {
    const { severity, tone } = severityOf(rule);
    return `<div class="rule-item ${rule.enabled === false ? 'rule-disabled' : ''}">
      <span class="finding-rule">${escapeHtml(rule.id)}</span>
      <div class="rule-main"><b>${escapeHtml(rule.title)}</b><span class="desc">${escapeHtml(rule.description)}</span></div>
      <div class="rule-tags">
        <span class="pill ${tone}">${escapeHtml(severity)}</span>
        ${rule.enabled === false ? '<span class="pill neutral">已停用</span>' : ''}
      </div>
    </div>`;
  }).join('') || '<div class="muted">暂无规则</div>';
  renderStage2Catalog(config.stage2 || null);
}
function renderStage2Catalog(stage2) {
  const partial = (stage2 && stage2.partial) || {};
  const dsl = (stage2 && stage2.dsl) || {};
  const semantic = (stage2 && stage2.semantic) || {};
  const partialRules = partial.rules || [];
  const dslRules = dsl.rules || [];
  $('stage2Count').textContent = `${(partial.total || partialRules.length) + (dsl.total || dslRules.length)} 条`;
  $('stage2Summary').innerHTML = [
    configFact('部分规则', `${partial.total || partialRules.length} 条`),
    configFact('规则 DSL', `v${dsl.version ?? 0} · ${dsl.total || dslRules.length} 条`),
    configFact('语义审核', semantic.enabled ? `已启用 · ${semantic.model || ''}` : '未启用'),
  ].join('');
  const rows = partialRules.concat(dslRules).map((rule) => {
    const severity = SEVERITY_NAMES[rule.severity] || rule.severity || (rule.layer === 'partial' ? '部分' : '—');
    const tone = SEVERITY_TONES[rule.severity] || 'neutral';
    return `<div class="rule-item">
      <span class="finding-rule">${escapeHtml(rule.id)}<span class="finding-layer ${escapeHtml(rule.layer || '')}">${escapeHtml(LAYER_LABELS[rule.layer] || '')}</span></span>
      <div class="rule-main"><b>${escapeHtml(rule.title)}</b><span class="desc">${escapeHtml(rule.description || rule.message || '')}${rule.requires ? ` · 需要：${escapeHtml(rule.requires)}` : ''}</span></div>
      <div class="rule-tags"><span class="pill ${tone}">${escapeHtml(severity)}</span></div>
    </div>`;
  }).join('');
  $('stage2Rules').innerHTML = rows || '<div class="muted">阶段 2 规则未加载</div>';
}
function renderStandardModuleConfig(config) {
  const standard = config.standard || { section_aliases: {}, fields: [] };
  const aliases = standard.section_aliases || {};
  const tech = aliases['技术要求'] || [];
  const method = aliases['试验方法'] || [];
  const fields = standard.fields || [];
  const required = fields.filter((field) => field.required).length;
  const exported = fields.filter((field) => field.export).length;
  $('stdConfigRev').textContent = `v${config.revision ?? 0}`;
  $('stdConfigInline').textContent = `技术要求：${tech.join(' / ') || '—'} · 试验方法：${method.join(' / ') || '—'}`;
  $('stdConfigMeta').textContent = `${configMetaText(config)}${configAccessText('standard')}`;
  $('stdConfigFacts').innerHTML = [
    configFact('技术要求别名', `${tech.length} 个`),
    configFact('试验方法别名', `${method.length} 个`),
    configFact('解析字段', `${fields.length} 个`),
    configFact('必填 / 导出', `${required} / ${exported}`),
  ].join('');
  $('stdConfigAliases').innerHTML = [
    aliasRow('技术要求', tech),
    aliasRow('试验方法', method),
  ].join('');
  $('stdConfigFields').innerHTML = fields.map((field) => `
    <tr>
      <td><span class="cell-main">${escapeHtml(field.label)}</span><span class="cell-sub">${escapeHtml(field.key)}</span></td>
      <td>${field.required ? '<span class="pill warn">缺失进复核</span>' : '<span class="muted">可选</span>'}</td>
      <td>${field.export ? '<span class="pill success">导出</span>' : '<span class="muted">不导出</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="3" class="muted">暂无字段</td></tr>';
}
function renderQuoteModuleConfig(config) {
  const quote = config.quote || { headers: {}, unit_map: {}, crm_columns: [] };
  const headers = quote.headers || {};
  const units = Object.entries(quote.unit_map || {});
  const columns = quote.crm_columns || [];
  const discount = Number(quote.default_discount ?? 60);
  const productTax = Number(quote.product_tax_rate ?? 0.16) * 100;
  const serviceTax = Number(quote.service_tax_rate ?? 0.06) * 100;
  const number = (value) => String(Math.round(value * 100) / 100);
  $('quoteConfigRev').textContent = `v${config.revision ?? 0}`;
  $('quoteConfigInline').textContent = `折扣 ${number(discount)}% · 产品税 ${number(productTax)}% · 服务税 ${number(serviceTax)}% · 单位映射 ${units.length} 条 · CRM ${columns.length} 列`;
  $('quoteConfigMeta').textContent = `${configMetaText(config)}${configAccessText('quote')}`;
  $('quoteConfigFacts').innerHTML = [
    configFact('默认折扣', `${number(discount)}%`),
    configFact('产品税率', `${number(productTax)}%`),
    configFact('服务税率', `${number(serviceTax)}%`),
    configFact('表头列', `${Object.keys(QUOTE_HEADER_NAMES).length} 类`),
  ].join('');
  $('quoteConfigHeaders').innerHTML = Object.entries(QUOTE_HEADER_NAMES).map(([key, label]) => {
    const values = headers[key] || [];
    const chips = values.map((value) => `<span class="chip">${escapeHtml(value)}</span>`).join('');
    return `<tr><td>${escapeHtml(label)}</td><td>${chips || '<span class="muted">—</span>'}</td></tr>`;
  }).join('');
  $('quoteConfigUnits').innerHTML = `<div class="alias-values">${units.map(([source, target]) => `<span class="chip">${escapeHtml(source)} → ${escapeHtml(target)}</span>`).join('') || '<span class="muted">—</span>'}</div>`;
  $('quoteConfigColumns').innerHTML = columns.map((column) => `
    <tr>
      <td><span class="cell-main">${escapeHtml(CONFIG_SOURCE_NAMES[column.source] || column.source)}</span><span class="cell-sub">${escapeHtml(column.source)}</span></td>
      <td>${escapeHtml(column.label)}</td>
      <td>${column.enabled ? '<span class="pill success">启用</span>' : '<span class="pill neutral">停用</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="3" class="muted">暂无 CRM 列</td></tr>';
  $('quotePolicyLine').textContent = `折扣与税率由管理端规则配置维护；当前默认折扣 ${number(discount)}%、产品税 ${number(productTax)}%、服务税 ${number(serviceTax)}%，不做自动推断。`;
}
function renderModuleConfig() {
  const wrapper = state.moduleConfig;
  if (!wrapper || !wrapper.config) {
    configErrorState(state.moduleConfigError ? `规则配置加载失败：${state.moduleConfigError}` : '正在加载管理端配置…');
    applyConfigAccessory();
    return;
  }
  renderReportModuleConfig(wrapper.config);
  renderStandardModuleConfig(wrapper.config);
  renderQuoteModuleConfig(wrapper.config);
  applyConfigAccessory();
}
async function loadModuleConfig() {
  try {
    state.moduleConfig = await jsonFetch('api/v1/config/effective');
    state.moduleConfigError = '';
  } catch (error) {
    state.moduleConfig = null;
    state.moduleConfigError = error.message;
  }
  renderModuleConfig();
}
function findingStatus(status) {
  if (status === 'fail') return { tone: 'danger', text: '失败' };
  if (status === 'warn') return { tone: 'warn', text: '提示' };
  if (status === 'pass') return { tone: 'success', text: '通过' };
  return { tone: 'neutral', text: '未检查' };
}
const REVIEW_LABELS = { accepted: '已采纳', ignored: '已忽略', false_positive: '误报' };
const REVIEW_TONES = { accepted: 'success', ignored: 'neutral', false_positive: 'warn' };
function reviewLabel(review) {
  return review && review.status ? (REVIEW_LABELS[review.status] || review.status) : '待复核';
}
function reviewTone(review) {
  return review && review.status ? (REVIEW_TONES[review.status] || 'neutral') : 'neutral';
}
function renderFinding(finding, review, reportId) {
  const status = findingStatus(finding.status);
  const location = finding.location || {};
  const locationText = [location.page ? `p${location.page}` : '', location.section || '', location.cell || '']
    .filter(Boolean).join(' / ');
  const layer = finding.layer && finding.layer !== 'deterministic'
    ? `<span class="finding-layer ${escapeHtml(finding.layer)}">${escapeHtml(LAYER_LABELS[finding.layer] || finding.layer)}</span>`
    : '';
  const actions = reportId ? `<div class="review-actions">
      <button type="button" class="btn btn-secondary btn-sm" data-review-status="accepted" data-review-rule="${escapeHtml(finding.rule_id || '')}" data-review-report="${escapeHtml(reportId)}">采纳</button>
      <button type="button" class="btn btn-secondary btn-sm" data-review-status="ignored" data-review-rule="${escapeHtml(finding.rule_id || '')}" data-review-report="${escapeHtml(reportId)}">忽略</button>
      <button type="button" class="btn btn-secondary btn-sm" data-review-status="false_positive" data-review-rule="${escapeHtml(finding.rule_id || '')}" data-review-report="${escapeHtml(reportId)}">误报</button>
    </div>` : '';
  return `<div class="finding ${escapeHtml(finding.status || 'skip')}">
    <span class="finding-rule">${escapeHtml(finding.rule_id || '—')}${layer}</span>
    <div class="finding-body">
      <div class="finding-title"><b>${escapeHtml(finding.title || '')}</b><span class="pill ${status.tone}">${status.text}</span><span class="pill ${reviewTone(review)}">${reviewLabel(review)}</span></div>
      ${locationText ? `<div class="finding-location"><b>定位：</b>${escapeHtml(locationText)}</div>` : ''}
      <p class="finding-msg">${escapeHtml(finding.message || '')}</p>
      ${finding.evidence ? `<div class="finding-evidence"><b>证据：</b>${escapeHtml(finding.evidence)}</div>` : ''}
      ${finding.suggestion ? `<div class="finding-suggestion"><b>建议：</b>${escapeHtml(finding.suggestion)}</div>` : ''}
      ${actions}
    </div>
  </div>`;
}
function renderStage2Strip(stage2) {
  const partial = stage2.partial || {};
  const dsl = stage2.dsl || {};
  const semantic = stage2.semantic || {};
  const statuses = partial.statuses || {};
  const semanticTone = semantic.status === 'ok' ? 'success' : semantic.status === 'error' ? 'warn' : 'neutral';
  const semanticText = semantic.status === 'ok' ? '已运行' : semantic.status === 'error' ? '调用失败' : '未启用';
  const semanticReason = semantic.status === 'ok' ? '' : ` · ${semantic.reason || '未配置'}`;
  return `<div class="stage2-strip">
    <span class="pill info">阶段 2</span>
    <span class="stage2-item"><b>部分规则</b> ${partial.count ?? 0} 条 · 未检查 ${statuses.skip ?? 0} · 失败 ${statuses.fail ?? 0}</span>
    <span class="stage2-item"><b>规则 DSL</b> v${dsl.version ?? 0} · ${dsl.rules ?? 0} 条 · 命中 ${Object.values(dsl.hits || {}).reduce((sum, value) => sum + Number(value || 0), 0)}</span>
    <span class="stage2-item"><span class="pill ${semanticTone}">语义${semanticText}</span><span class="muted">${escapeHtml(semanticReason)}</span></span>
  </div>`;
}
function renderReport(data) {
  state.currentReportData = data;
  const summary = data.summary || {};
  const findings = data.findings || [];
  const reviews = data.reviews || {};
  const coverage = data.review_coverage || {
    reviewed: findings.filter((finding) => reviews[finding.rule_id]).length,
    total: findings.length,
  };
  const severityFilter = state.reportSeverityFilter || '';
  const groups = [
    { key: 'fail', label: '需要处理' },
    { key: 'warn', label: '建议确认' },
    { key: 'pass', label: '已通过' },
    { key: 'skip', label: '未检查' },
  ];
  const pills = groups
    .filter((group) => summary[group.key])
    .map((group) => `<span class="pill ${findingStatus(group.key).tone}">${findingStatus(group.key).text} ${summary[group.key]}</span>`)
    .join('');
  const visible = severityFilter ? findings.filter((finding) => finding.severity === severityFilter) : findings;
  const findingGroups = groups.map((group) => {
    const items = visible.filter((finding) => finding.status === group.key);
    if (!items.length) return '';
    return `<div class="finding-group">
      <div class="group-head"><span>${group.label}</span><span class="count">${items.length}</span></div>
      <div class="finding-list">${items.map((finding) => renderFinding(finding, reviews[finding.rule_id], data.report_id)).join('')}</div>
    </div>`;
  }).join('') || '<div class="muted pad-8">当前筛选下没有问题</div>';
  const severityOptions = ['high', 'medium', 'low', 'info'].map((value) => {
    const label = { high: '高', medium: '中', low: '低', info: '提示' }[value];
    return `<option value="${value}"${severityFilter === value ? ' selected' : ''}>${label}</option>`;
  }).join('');
  const meta = [
    data.project ? `项目 ${data.project}` : '',
    data.created_at ? `审核于 ${formatTime(data.created_at)}` : '',
  ].filter(Boolean).map(escapeHtml).join(' · ');
  const actions = data.report_id ? `<div class="report-actions">
      <span class="report-actions-meta">记录 ${escapeHtml(data.report_id)}${data.created_by ? ` · ${escapeHtml(data.created_by)}` : ''}</span>
      <span class="grow"></span>
      <button type="button" class="btn btn-secondary btn-sm" data-report-export="${escapeHtml(data.report_id)}">${ic('download')}导出 Excel</button>
      <button type="button" class="btn btn-secondary btn-sm" data-report-source="${escapeHtml(data.report_id)}">${ic('file')}下载原文</button>
    </div>` : '';
  $('reportResult').innerHTML = `
    <div class="summary-band">
      <span class="file-badge">${ic('file')}</span>
      <div><span class="file-name">${escapeHtml(data.file || '已上传报告')}</span><span class="file-sub">${escapeHtml(TEMPLATE_NAMES[data.template] || data.template || '报告模板')} · 共 ${escapeHtml(summary.total ?? findings.length)} 条规则${meta ? ` · ${meta}` : ''}</span></div>
      <div class="summary-pills">${pills}</div>
    </div>
    ${data.stage2 ? renderStage2Strip(data.stage2) : ''}
    ${actions}
    <div class="review-toolbar">
      <label class="muted">严重级别 <select id="findingSeverityFilter"><option value="">全部</option>${severityOptions}</select></label>
      <span class="pill ${coverage.covered ? 'success' : 'warn'}">复核 ${coverage.reviewed}/${coverage.total}</span>
      <span class="muted">每条问题需人工状态：采纳 / 忽略 / 误报</span>
    </div>
    <div class="kpi-grid">
      ${kpi(summary.pass ?? 0, '通过', 'success', 'check')}
      ${kpi(summary.warn ?? 0, '提示', 'warn', 'alert')}
      ${kpi(summary.fail ?? 0, '失败', 'danger', 'x')}
      ${kpi(summary.skip ?? 0, '未检查', 'neutral', 'clock')}
    </div>
    ${findingGroups}
    <details class="raw-details"><summary>查看原始 JSON</summary><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></details>`;
  $('reportEmpty').classList.add('hidden');
  $('reportResult').classList.remove('hidden');
}
function reportPills(summary) {
  return [
    summary.fail ? `<span class="pill danger">失败 ${summary.fail}</span>` : '',
    summary.warn ? `<span class="pill warn">提示 ${summary.warn}</span>` : '',
    summary.pass ? `<span class="pill success">通过 ${summary.pass}</span>` : '',
  ].filter(Boolean).join('');
}
function renderReportBatch(data) {
  const items = data.items || [];
  const summary = data.summary || {};
  const rows = items.map((item) => {
    if (!item.ok) {
      return `<tr>
        <td><span class="cell-main">${escapeHtml(item.file)}</span></td>
        <td><span class="pill danger">失败</span></td>
        <td colspan="2"><span class="muted">${escapeHtml(item.error || '审核失败')}</span></td>
      </tr>`;
    }
    return `<tr>
      <td><span class="cell-main">${escapeHtml(item.file)}</span><span class="cell-sub">${escapeHtml(item.report_id)}</span></td>
      <td><span class="pill success">已审核</span></td>
      <td><span class="report-history-pills">${reportPills(item.summary || {})}</span></td>
      <td><button type="button" class="btn btn-secondary btn-sm" data-report-id="${escapeHtml(item.report_id)}">查看</button></td>
    </tr>`;
  }).join('');
  const pills = [
    summary.fail ? `<span class="pill danger">失败 ${summary.fail}</span>` : '',
    summary.warn ? `<span class="pill warn">提示 ${summary.warn}</span>` : '',
    summary.stored ? `<span class="pill success">成功 ${summary.stored}</span>` : '',
  ].filter(Boolean).join('');
  $('reportResult').innerHTML = `
    <div class="summary-band">
      <span class="file-badge">${ic('layers')}</span>
      <div><span class="file-name">批量审核 · ${escapeHtml(summary.total ?? items.length)} 份报告</span><span class="file-sub">批次 ${escapeHtml(data.batch_id)} · ${data.project ? `项目 ${escapeHtml(data.project)} · ` : ''}${escapeHtml(summary.failed ?? 0)} 份失败</span></div>
      <div class="summary-pills">${pills}</div>
    </div>
    <div class="report-actions">
      <span class="report-actions-meta">批次 ${escapeHtml(data.batch_id)}</span>
      <span class="grow"></span>
      <button type="button" class="btn btn-secondary btn-sm" data-batch-export="${escapeHtml(data.batch_id)}">${ic('download')}导出汇总 Excel</button>
    </div>
    <div class="kpi-grid">
      ${kpi(summary.total ?? items.length, '提交文件', 'info', 'file')}
      ${kpi(summary.stored ?? 0, '已留存', 'success', 'check')}
      ${kpi(summary.failed ?? 0, '失败', 'danger', 'x')}
      ${kpi(summary.fail ?? 0, '失败规则', 'warn', 'alert')}
    </div>
    <div class="table-wrap batch-table">
      <table>
        <thead><tr><th>文件</th><th>结果</th><th>问题汇总</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  $('reportEmpty').classList.add('hidden');
  $('reportResult').classList.remove('hidden');
}
function renderReportHistory() {
  const box = $('reportHistory');
  const page = state.reportPage;
  const total = page.total;
  const shown = state.reports.length;
  const pages = Math.max(1, Math.ceil(total / page.limit));
  $('reportHistoryCount').textContent = total;
  $('reportHistoryPager').classList.toggle('hidden', total <= page.limit);
  $('reportHistoryMeta').textContent = total
    ? `共 ${total} 条，显示第 ${page.offset + 1}–${Math.min(page.offset + shown, total)} 条`
    : '共 0 条';
  $('reportHistoryPage').textContent = `${Math.floor(page.offset / page.limit) + 1} / ${pages}`;
  $('reportHistoryPrev').disabled = page.offset <= 0;
  $('reportHistoryNext').disabled = page.offset + page.limit >= total;
  if (!state.reports.length) {
    box.innerHTML = page.query
      ? `<div class="empty compact"><b>没有匹配的审核记录</b><p>调整关键词后重试。</p></div>`
      : `<div class="empty compact"><b>还没有审核记录</b><p>完成一次报告审核后，结果会出现在这里。</p></div>`;
    return;
  }
  box.innerHTML = state.reports.map((report) => {
    const active = state.currentReport && state.currentReport.id === report.id ? ' active' : '';
    const batch = report.batch_id ? '<span class="pill neutral">批量</span>' : '';
    return `<button type="button" class="report-history-item${active}" data-report-id="${escapeHtml(report.id)}">
      <span class="report-history-main">
        <span class="cell-main">${escapeHtml(report.file_name || '未命名报告')}</span>
        <span class="cell-sub">${escapeHtml(formatTime(report.created_at))} · ${escapeHtml(report.created_by || '—')}${report.project ? ` · 项目 ${escapeHtml(report.project)}` : ''}</span>
      </span>
      <span class="report-history-pills">${batch}${reportPills(report.summary || {})}</span>
    </button>`;
  }).join('');
}
async function loadReportInputs() {
  const pill = $('reportInputsPill');
  const body = $('reportInputsBody');
  try {
    const data = await jsonFetch('api/v1/reports/inputs/status');
    if (!data.configured) {
      pill.className = 'pill neutral';
      pill.textContent = '未配置';
      body.innerHTML = '<p class="muted">未设置 P0_REPORT_INPUTS_DIR；当前只运行报告内确定性规则（R01 起），X01–X04 跨文档检查停用。</p>';
      return;
    }
    const counts = data.counts || {};
    const errors = (data.errors || []).length;
    const warnings = (data.warnings || []).length;
    pill.className = `pill ${errors ? 'danger' : warnings ? 'warn' : 'success'}`;
    pill.textContent = errors ? `有错误 ${errors}` : warnings ? `有警告 ${warnings}` : '数据就绪';
    const facts = [
      ['任务单', `${counts.task_orders ?? 0} 行`],
      ['原始记录', `${counts.original_records ?? 0} 行`],
      ['样品', `${counts.samples ?? 0} 行`],
      ['设备台账', `${counts.equipment ?? 0} 行`],
    ].map(([label, value]) => configFact(label, value)).join('');
    const notes = [];
    if (errors) notes.push(`错误：${errors} 项（${(data.errors || [])[0] || ''}）`);
    if (warnings) notes.push(`警告：${warnings} 项（${(data.warnings || [])[0] || ''}）`);
    if (!errors && !warnings) notes.push('四类数据已加载，审核将追加 X01–X04。');
    body.innerHTML = `<div class="config-facts">${facts}</div><p class="muted mt-8">${notes.map((item) => escapeHtml(item)).join(' ')}</p>`;
  } catch (error) {
    pill.className = 'pill danger';
    pill.textContent = '检查失败';
    body.innerHTML = `<p class="muted">${escapeHtml(error.message || '无法读取数据状态')}</p>`;
  }
}
async function reviewFinding(reportId, ruleId, status) {
  const data = await jsonFetch(`api/v1/reports/${encodeURIComponent(reportId)}/reviews`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rule_id: ruleId, status }),
  });
  if (state.currentReportData) {
    state.currentReportData.reviews = data.reviews || {};
    state.currentReportData.review_coverage = data.review_coverage;
    renderReport(state.currentReportData);
  }
  toast(`已标记 ${ruleId}：${REVIEW_LABELS[status] || status}`, 'info');
}
async function waitForReportTask(taskId) {
  const stageLabels = { queued: '排队', load: '读取文件', audit: '审核中', save: '保存结果' };
  for (let attempt = 0; attempt < 150; attempt += 1) {
    const task = await jsonFetch(`api/v1/tasks/${encodeURIComponent(taskId)}`);
    const stage = stageLabels[task.stage] || task.stage || '处理中';
    $('reportStatus').textContent = `后台审核中：${stage} ${Number(task.progress) || 0}%`;
    if (task.status === 'succeeded') return task.result;
    if (task.status === 'failed' || task.status === 'cancelled') {
      throw new Error(task.error || (task.status === 'cancelled' ? '后台审核已取消' : '后台审核失败'));
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  throw new Error('后台审核超时，请稍后在审核历史查看结果');
}
async function loadReports() {
  const page = state.reportPage;
  const params = new URLSearchParams({
    limit: String(page.limit),
    offset: String(page.offset),
  });
  if (page.query) params.set('q', page.query);
  const data = await jsonFetch(`api/v1/reports?${params.toString()}`);
  state.reports = data.reports || [];
  page.total = data.total ?? state.reports.length;
  page.offset = data.offset ?? page.offset;
  renderReportHistory();
}
async function loadReport(reportId) {
  const record = await jsonFetch(`api/v1/reports/${encodeURIComponent(reportId)}`);
  state.currentReport = record;
  const result = record.result || {};
  renderReport({
    ...result,
    file: result.file || record.file_name,
    report_id: record.id,
    project: record.project,
    created_at: record.created_at,
    created_by: record.created_by,
  });
  $('reportStatus').textContent = `已打开历史记录 ${record.id}`;
  renderReportHistory();
}
function renderQuoteHistory() {
  const box = $('quoteHistory');
  $('quoteHistoryCount').textContent = state.quotesTotal;
  if (!state.quotes.length) {
    box.innerHTML = `<div class="empty compact"><b>还没有报价记录</b><p>生成一次报价后，这里会保留 CRM 草稿与销售报价表的下载入口。</p></div>`;
    return;
  }
  box.innerHTML = state.quotes.map((quote) => {
    const summary = quote.summary || {};
    const crm = quote.crm || {};
    const lines = quote.lines ?? summary.lines ?? 0;
    const priced = quote.priced_lines ?? summary.priced_lines ?? 0;
    const sales = Boolean(quote.has_sales_quote);
    const salesPill = sales
      ? `<span class="pill ${quote.verification_ok ? 'success' : 'warn'}">${quote.verification_ok ? '销售表·自检通过' : '销售表'}</span>`
      : '<span class="pill warn">仅 CRM 草稿</span>';
    const crmPill = crm.enabled
      ? `<span class="pill ${crm.reconciled ? 'success' : 'warn'}">对账 ${escapeHtml(crm.matched ?? 0)}/${escapeHtml(lines)}</span>`
      : '';
    return `<div class="quote-history-item">
      <div class="quote-history-main">
        <span class="cell-main">${escapeHtml(quote.source_name || '未命名报价')}</span>
        <span class="cell-sub">${escapeHtml(formatTime(quote.created_at))} · ${escapeHtml(quote.created_by || '—')}${quote.project ? ` · 项目 ${escapeHtml(quote.project)}` : ''} · ${escapeHtml(lines)} 行 / ${escapeHtml(priced)} 有价${quote.parse_method ? ` · ${escapeHtml(quote.parse_method)}` : ''}</span>
      </div>
      <div class="quote-history-pills">
        ${crmPill}${salesPill}
        <button type="button" class="btn btn-secondary btn-sm" data-quote-download="${escapeHtml(quote.quote_id)}" data-kind="crm">下载 CRM</button>
        ${sales ? `<button type="button" class="btn btn-secondary btn-sm" data-quote-download="${escapeHtml(quote.quote_id)}" data-kind="sales">下载销售表</button>` : ''}
      </div>
    </div>`;
  }).join('');
}
async function loadQuotes() {
  const data = await jsonFetch('api/v1/quotes?limit=20');
  state.quotes = data.quotes || [];
  state.quotesTotal = data.total ?? state.quotes.length;
  renderQuoteHistory();
}
function renderRecords(data) {
  const findings = data.findings || [];
  const summary = data.summary || {};
  if (!findings.length) {
    $('recordsResult').innerHTML = `<div class="banner info">${ic('check')}<span>${escapeHtml(summary.total ?? 0)} 条记录全部通过均值、日期、判定与编号唯一性检查。</span></div>`;
    return;
  }
  $('recordsResult').innerHTML = `
    <div class="records-summary"><span class="pill danger">${escapeHtml(summary.fail ?? findings.length)} 项问题</span><span class="pill neutral">${escapeHtml(summary.total ?? 0)} 条记录</span></div>
    <div class="finding-list">${findings.map(renderFinding).join('')}</div>`;
}

/* ---------------------------------------------------------------- standards */
function renderStdList() {
  const query = ($('stdSearch').value || '').trim().toLowerCase();
  const items = state.standards.filter((standard) => {
    const haystack = `${standard.standard_no || ''} ${standard.name || ''} ${standard.project || ''}`.toLowerCase();
    return !query || haystack.includes(query);
  });
  if (!items.length) {
    $('stdList').innerHTML = `<div class="muted pad-8">${state.standards.length ? '没有匹配的标准' : '还没有已解析标准'}</div>`;
    return;
  }
  $('stdList').innerHTML = items.map((standard) => {
    const active = state.current && state.current.id === standard.id ? 'active' : '';
    const published = standard.published ? '<span class="pill success">已发布</span>' : '<span class="pill neutral">未发布</span>';
    return `<button type="button" class="std-item ${active}" data-std-id="${escapeHtml(standard.id)}">
      <div class="std-item-top"><span class="std-no">${escapeHtml(standard.standard_no || '未识别标准号')}</span>${published}</div>
      <span class="std-name" title="${escapeHtml(standard.name || '')}">${escapeHtml(standard.name || '未命名标准')}</span>
      <div class="std-meta"><span>${escapeHtml(standard.items ?? 0)} 项</span><span>${escapeHtml(standard.pages ?? 0)} 页</span>${standard.project ? `<span>项目 ${escapeHtml(standard.project)}</span>` : ''}</div>
    </button>`;
  }).join('');
}
async function loadStandards(preferredId) {
  const data = await jsonFetch('api/v1/standards');
  state.standards = data.standards || [];
  $('stdCount').textContent = state.standards.length;
  $('stdHeadPill').textContent = `${state.standards.length} 份标准`;
  renderStdList();
  if (!state.standards.length) {
    state.current = null;
    $('stdEmpty').classList.remove('hidden');
    $('stdDetail').classList.add('hidden');
    return;
  }
  const wanted = preferredId || (state.current && state.current.id) || params.get('std');
  const exists = state.standards.some((standard) => standard.id === wanted);
  await loadStandard(exists ? wanted : state.standards[0].id);
}
async function loadStandard(id, keepClause) {
  if (!id) return;
  $('publishTaskBox').classList.add('hidden');
  const [data, versions] = await Promise.all([
    jsonFetch(`api/v1/standards/${encodeURIComponent(id)}`),
    jsonFetch(`api/v1/standards/${encodeURIComponent(id)}/versions`).catch(() => ({ versions: [] })),
  ]);
  state.current = data;
  state.versions = versions.versions || [];
  const listed = state.standards.find((standard) => standard.id === data.id);
  if (listed) {
    listed.published = Boolean(data.published);
    listed.revision = data.revision ?? 0;
    listed.items = (data.summary || {}).items ?? listed.items;
    listed.pages = (data.meta || {}).pages ?? listed.pages;
  }
  state.selectedIndex = keepClause ? (data.items || []).findIndex((item) => item.clause === keepClause) : -1;
  renderStdDetail();
  renderStdList();
}
const TASK_STAGE_LABELS = {
  queued: '排队中', starting: '启动中', extract: '提取文本层', ocr: '扫描件 OCR',
  parse: '结构化解析', parsed: '解析完成', save: '保存结果', load: '读取标准',
  indexing: '写入知识库', succeeded: '已完成', failed: '失败', cancelled: '已取消', requeued: '重新排队',
};
const TASK_STATUS_LABELS = { queued: '排队中', running: '进行中', succeeded: '已完成', failed: '失败', cancelled: '已取消' };
const TASK_STATUS_PILL = { queued: 'neutral', running: 'info', succeeded: 'success', failed: 'danger', cancelled: 'neutral' };

function renderTask(prefix, task) {
  if (!task || !task.id) return;
  const box = $(`${prefix}Box`);
  if (!box) return;
  state[`${prefix}Id`] = task.id;
  box.classList.remove('hidden', 'is-queued', 'is-running', 'is-succeeded', 'is-failed', 'is-cancelled');
  box.classList.add(`is-${task.status || 'queued'}`);
  const pill = $(`${prefix}Pill`);
  pill.textContent = TASK_STATUS_LABELS[task.status] || task.status || '';
  pill.className = `pill ${TASK_STATUS_PILL[task.status] || 'neutral'}`;
  const percent = task.status === 'succeeded' ? 100 : Math.max(0, Math.min(100, Number(task.progress || 0)));
  $(`${prefix}Bar`).style.width = `${percent}%`;
  const stage = TASK_STAGE_LABELS[task.stage] || task.stage || '';
  $(`${prefix}Stage`).textContent = task.status === 'running' ? `${stage} · ${percent}%` : stage;
  const cancel = $(`${prefix}Cancel`);
  cancel.classList.toggle('hidden', !['queued', 'running'].includes(task.status));
  cancel.disabled = Boolean(task.cancel_requested);
  cancel.textContent = task.cancel_requested ? '取消中…' : '取消任务';
  const error = $(`${prefix}Error`);
  if (task.status === 'failed' && task.error) {
    error.textContent = `任务失败：${task.error}`;
    error.classList.remove('hidden');
  } else {
    error.classList.add('hidden');
  }
}

function pollTask(taskId, prefix) {
  if (state[`${prefix}Timer`]) clearTimeout(state[`${prefix}Timer`]);
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const task = await jsonFetch(`api/v1/tasks/${encodeURIComponent(taskId)}`);
        renderTask(prefix, task);
        if (['queued', 'running'].includes(task.status)) {
          state[`${prefix}Timer`] = setTimeout(tick, 1000);
        } else {
          resolve(task);
        }
      } catch (error) {
        reject(error);
      }
    };
    tick();
  });
}

async function cancelTask(prefix) {
  const taskId = state[`${prefix}Id`];
  if (!taskId) return;
  try {
    const task = await jsonFetch(`api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' });
    renderTask(prefix, task);
    toast('已请求取消任务', 'info');
  } catch (error) {
    showError(error);
  }
}

async function resumeLatestTask() {
  try {
    const data = await jsonFetch('api/v1/tasks?limit=1');
    const task = (data.tasks || [])[0];
    if (!task) return;
    if (task.kind === 'standard_parse') {
      renderTask('stdTask', task);
      if (['queued', 'running'].includes(task.status)) {
        pollTask(task.id, 'stdTask').then((finished) => {
          if (finished.status === 'succeeded' && finished.result && finished.result.standard_id) {
            loadStandards(finished.result.standard_id).catch(showError);
          }
        }).catch(() => {});
      }
      return;
    }
    if (task.kind === 'standard_publish' && state.current && state.current.id === task.standard_id) {
      renderTask('publishTask', task);
      if (['queued', 'running'].includes(task.status)) {
        pollTask(task.id, 'publishTask').then((finished) => {
          if (finished.status === 'succeeded') loadStandard(task.standard_id).catch(showError);
        }).catch(() => {});
      }
    }
  } catch (error) {
    /* task queue is optional; a 503/404 simply means nothing to resume */
  }
}

function renderReviewHistory(clause) {
  const entries = (state.current.review_history || []).filter((entry) => entry.clause === clause).slice(-3).reverse();
  if (!entries.length) {
    $('reviewHistory').innerHTML = '';
    return;
  }
  $('reviewHistory').innerHTML = `<h4>最近复核</h4>${entries.map((entry) => `
    <div class="history-item"><b>${escapeHtml(FIELD_NAMES[entry.field] || entry.field || '字段')}</b> · ${escapeHtml(entry.reviewer || '')} · ${escapeHtml(formatTime(entry.at))}<br>${escapeHtml(String(entry.after || '').slice(0, 90))}</div>`).join('')}`;
}
function renderReviewFeedback() {
  const samples = (state.current && state.current.review_feedback) || [];
  $('feedbackCount').textContent = samples.length;
  $('reviewFeedbackList').innerHTML = samples.length ? samples.slice(-8).reverse().map((sample) => {
    const detail = sample.action === 'supplement'
      ? `补充 ${sample.clause} ${sample.after}`
      : sample.action === 'confirm' ? `${sample.clause} ${sample.name} · 确认无误`
      : `${sample.clause} ${FIELD_NAMES[sample.field] || ''}：${String(sample.before || '（空）').slice(0, 40)} → ${String(sample.after || '（空）').slice(0, 40)}`;
    return `<div class="feedback-sample"><span class="pill info">${escapeHtml(FEEDBACK_ACTIONS[sample.action] || '复核')}</span><span class="feedback-sample-main">${escapeHtml(detail)}</span><span class="muted">${escapeHtml(sample.reviewer || '')} · ${escapeHtml(formatTime(sample.at))}</span>${sample.reason ? `<span class="feedback-sample-reason">原解析原因：${escapeHtml(sample.reason)}</span>` : ''}</div>`;
  }).join('') : '<div class="muted">还没有反馈样本；保存修正、确认或补充后会自动留存。</div>';
}
function selectItem(index) {
  if (!state.current || !state.current.items || !state.current.items[index]) return;
  state.selectedIndex = index;
  const item = state.current.items[index];
  $$('#itemsBody tr[data-i]').forEach((row) => row.classList.toggle('selected', Number(row.dataset.i) === index));
  $('editor').classList.add('hidden');
  $('editorFields').classList.remove('hidden');
  $('editorClause').textContent = `条款 ${item.clause || ''}`;
  $('editorTitle').textContent = item.name || '未命名条目';
  $('editName').value = item.name || '';
  $('editReq').value = item.requirement || '';
  $('editMethod').value = item.method || '';
  $('saveStatus').textContent = '';
  const status = $('editorStatus');
  status.textContent = item.reviewed ? '已复核' : '待复核';
  status.className = `pill ${item.reviewed ? 'success' : 'warn'}`;
  $('confirmBtn').textContent = item.reviewed ? '取消确认' : '确认无误';
  $('addForm').classList.add('hidden');
  const references = (item.references || []).map((reference) => `${reference.ref || ''} → 现行 ${reference.current || ''}`).join('；');
  $('refs').textContent = references ? `原文引用国标：${references}` : '';
  renderReviewHistory(item.clause);
  if (window.innerWidth < 1024) $('editorFields').scrollIntoView({ behavior: 'smooth', block: 'center' });
}
function renderStdVersions() {
  const strip = $('stdVersionStrip');
  const list = $('stdVersionList');
  const versions = state.versions || [];
  if (!versions.length) {
    strip.classList.add('hidden');
    list.innerHTML = '';
    return;
  }
  strip.classList.remove('hidden');
  list.innerHTML = versions.map((version) => {
    const current = Boolean(version.current);
    const canRollback = !current && version.rollbackable && can('standard:review');
    const tail = current
      ? '<span class="pill success">当前</span>'
      : canRollback
        ? `<button type="button" class="link-btn" data-rollback="${escapeHtml(version.version)}">回滚</button>`
        : '<span class="muted">无快照</span>';
    return `<span class="version-chip${current ? ' current' : ''}">
      <b>r${escapeHtml(version.version)}</b>
      <span>${escapeHtml(formatTime(version.created_at))}</span>
      <span>${escapeHtml(version.reviewer || '—')}</span>
      <span class="muted">${escapeHtml(version.items ?? 0)} 项</span>
      ${tail}
    </span>`;
  }).join('');
}
function renderJudgmentRules(judgment) {
  const box = $('stdRules');
  if (!box) return;
  const rules = (judgment && judgment.rules) || [];
  const pending = (judgment && judgment.pending) || [];
  if (!rules.length && !pending.length) {
    box.innerHTML = '';
    return;
  }
  const pendingItems = pending.map((entry) => {
    const evidence = [
      entry.page ? `第 ${entry.page} 页` : '',
      entry.table_row !== null && entry.table_row !== undefined ? `第 ${Number(entry.table_row) + 1} 行` : '',
      entry.column ? `列 ${entry.column}` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="pending-limit">
      <span class="pill warn">待确认</span>
      <span class="finding-rule">${escapeHtml(entry.clause || '—')}</span>
      <b>${escapeHtml(entry.name || '未命名条目')}</b>
      <span class="pending-limit-value">${escapeHtml(entry.raw || '—')}${entry.unit ? ` ${escapeHtml(entry.unit)}` : ''}</span>
      <span class="pending-limit-desc">${escapeHtml(entry.reason || '')}${evidence ? ` · ${escapeHtml(evidence)}` : ''}</span>
    </div>`;
  }).join('');
  box.innerHTML = `<div class="banner ${pending.length ? 'warn' : 'info'}">
    ${ic(pending.length ? 'alert' : 'check')}
    <div class="judgment-box">
      <b>判定规则草稿</b>
      <div>高置信限值生成 ${escapeHtml(rules.length)} 条规则草稿；${pending.length
        ? `${escapeHtml(pending.length)} 条低置信或缺少比较方向的限值已隔离，不参与判定，请人工确认。`
        : '没有待确认限值。'}</div>
      ${pending.length ? `<div class="pending-limit-list">${pendingItems}</div>` : ''}
    </div>
  </div>`;
}

function renderStdDetail() {
  const current = state.current;
  if (!current) return;
  $('stdEmpty').classList.add('hidden');
  $('stdDetail').classList.remove('hidden');
  const meta = current.meta || {};
  const summary = current.summary || {};
  const judgment = current.judgment_rules || {};
  const judgmentRules = judgment.rules || [];
  const pendingLimits = judgment.pending || [];
  $('stdNo').textContent = meta.standard_no || 'STANDARD';
  $('stdName').textContent = meta.name || '未命名标准';
  $('stdMeta').innerHTML = [
    meta.pages ? `${meta.pages} 页` : '',
    summary.parse_ms ? `解析 ${summary.parse_ms} ms` : '',
    `修订 v${current.revision ?? 0}`,
    meta.project ? `项目 ${meta.project}` : '不限定项目',
    current.updated_at ? `更新 ${formatTime(current.updated_at).slice(0, 10)}` : '',
  ].filter(Boolean).map((text) => `<span>${escapeHtml(text)}</span>`).join('');
  const badges = [];
  badges.push(current.published ? '<span class="pill success">已发布</span>' : '<span class="pill warn">未发布</span>');
  if (current.weknora && current.weknora.knowledge_id) badges.push(`<span class="pill info">${ic('check')}WeKnora 已同步</span>`);
  if (meta.project) badges.push(`<span class="pill neutral">${ic('folder')}项目 ${escapeHtml(meta.project)}</span>`);
  if (meta.confidential) badges.push(`<span class="pill warn">${ic('lock')}受限标准</span>`);
  $('stdBadges').innerHTML = badges.join('');
  $('stdStats').innerHTML = [
    kpi(summary.items ?? 0, '实检项目', 'info', 'layers'),
    kpi(summary.clauses_traced ?? 0, '条款可溯源', 'info', 'activity'),
    kpi(summary.reviewed_items ?? 0, '已复核', 'success', 'check'),
    kpi(summary.pages ?? meta.pages ?? 0, '标准页数', 'neutral', 'file'),
    kpi(judgmentRules.length, '判定规则草稿', 'info', 'shield'),
    kpi(pendingLimits.length, '限值待确认', pendingLimits.length ? 'warn' : 'neutral', 'alert'),
  ].join('');
  const reviewed = summary.reviewed_items || 0;
  const total = summary.items || 0;
  const percent = total ? Math.round((reviewed / total) * 100) : 0;
  $('reviewProgress').innerHTML = `<span class="progress-label">复核进度</span><div class="progress"><i style="width:${percent}%"></i></div><span class="progress-label">${reviewed} / ${total}（${percent}%）</span>`;
  const warnings = current.warnings || [];
  $('stdWarnings').innerHTML = warnings.length
    ? `<div class="banner warn">${ic('alert')}<div><b>解析提示</b><div>${warnings.map(escapeHtml).join('<br>')}</div></div></div>`
    : '';
  renderJudgmentRules(judgment);
  const items = current.items || [];
  $('itemsBody').innerHTML = items.map((item, index) => {
    const selected = index === state.selectedIndex ? 'selected' : '';
    const itemPending = (item.limits || []).filter((limit) => limit.mapping_confidence === 'low').length;
    return `<tr data-i="${index}" class="${selected}">
      <td class="muted">${escapeHtml(item.seq ?? index + 1)}</td>
      <td><span class="pill neutral">${escapeHtml(item.clause || '—')}</span></td>
      <td><span class="cell-main">${escapeHtml(item.name || '—')}</span><span class="cell-sub">方法 ${escapeHtml(item.method_clause || '—')}</span></td>
      <td>${escapeHtml(item.method_clause || '—')}</td>
      <td>${escapeHtml(item.page ?? '—')}</td>
      <td>${item.reviewed ? '<span class="pill success">已复核</span>' : '<span class="pill info">待复核</span>'}${itemPending ? `<span class="pill warn">${escapeHtml(itemPending)} 条限值待确认</span>` : ''}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="muted">没有抽取到项目</td></tr>';
  $('exportBtn').disabled = !items.length;
  $('publishBtn').disabled = !items.length || Boolean(current.published);
  $('publishBtn').innerHTML = current.published
    ? `${ic('check')}已发布`
    : `${ic('upload')}${current.unpublished_at ? '重新发布' : '发布到知识库'}`;
  $('unpublishBtn').classList.toggle(
    'hidden',
    !current.published || !can('standard:publish')
  );
  renderStdVersions();
  renderReviewFeedback();
  if (state.selectedIndex >= 0 && items[state.selectedIndex]) {
    selectItem(state.selectedIndex);
  } else {
    $('editorFields').classList.add('hidden');
    $('editor').classList.remove('hidden');
  }
}

/* ---------------------------------------------------------------- quote */
function renderQuote(data) {
  const summary = data.summary || {};
  const preview = data.preview || {};
  const verification = data.sales_quote_verification;
  const parseMethod = summary.parse_method || 'Excel 表格';
  const unpriced = Number(summary.unpriced_lines || 0);
  const partial = unpriced > 0;
  const skipReason = data.sales_quote_skipped_reason || '';
  let badge;
  if (verification) badge = verification.ok ? '<span class="pill success">金额自检通过</span>' : '<span class="pill danger">金额自检失败</span>';
  else if (partial) badge = `<span class="pill warn">${unpriced} 行待补单价</span>`;
  else badge = '<span class="pill warn">未提供模板，仅生成 CRM 草稿</span>';
  const verifyPills = verification && verification.checks
    ? `<div class="verify-list">${verification.checks.map((check) => `<span class="pill ${check.ok ? 'success' : 'danger'}">${escapeHtml(check.cell)} ${escapeHtml(check.field)}</span>`).join('')}</div>`
    : '';
  const warnings = Array.isArray(summary.warnings) ? summary.warnings : [];
  const warningBlock = partial
    ? `<div class="banner warn">${ic('alert')}<span><b>${unpriced}/${summary.lines || 0}</b> 行没有单价：CRM 草稿已标注「待补单价」，本次不生成销售报价表；补齐单价后可重新生成。</span></div>`
    : (skipReason ? `<div class="banner info">${ic('file')}<span>${escapeHtml(skipReason)}</span></div>` : '');
  const warningList = warnings.length
    ? `<ul class="check-list quote-warnings">${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join('')}</ul>`
    : '';
  $('quoteResult').innerHTML = `
    ${warningBlock}
    <div class="quote-hero">
      <div class="quote-price"><span>${partial ? '已计价部分（含税）' : '最终价格（含税）'}</span><b>¥ ${money(summary.final_price)}</b><small>销售报价 ${money(summary.sales_amount)} 元 · 服务税额 ${money(summary.service_tax)} 元</small></div>
      <div class="quote-badges">${badge}<span class="pill info">${escapeHtml(summary.sample_name || '已生成报价')}</span><span class="pill neutral">${escapeHtml(parseMethod)}</span></div>
    </div>
    <div class="kpi-grid five">
      ${kpi(summary.lines ?? 0, '解析明细', 'neutral', 'file')}
      ${kpi(`${summary.priced_lines}/${summary.lines}`, '含单价行', partial ? 'warn' : 'neutral', 'check')}
      ${kpi(`¥ ${money(summary.subtotal)}`, partial ? '已计价小计' : '明细小计', 'neutral', 'calc')}
      ${kpi(`¥ ${money(summary.service_amount)}`, '服务金额', 'neutral', 'shield')}
      ${kpi(`¥ ${money(summary.total_with_tax)}`, '税价合计', 'neutral', 'chart')}
    </div>
    <div class="quote-actions">
      <button type="button" class="btn btn-primary" data-download="${escapeHtml(data.quote_id)}" data-kind="crm">${ic('download')}下载 CRM 报价草稿</button>
      ${data.files && data.files.sales_quote ? `<button type="button" class="btn btn-secondary" data-download="${escapeHtml(data.quote_id)}" data-kind="sales">${ic('download')}下载销售报价表</button>` : ''}
    </div>
    <div class="breakdown">${Object.entries(preview).map(([label, value]) => `<div class="breakdown-row"><span>${escapeHtml(label)}</span><b>${typeof value === 'number' ? money(value) : escapeHtml(value)}</b></div>`).join('')}</div>
    ${warningList}
    ${verifyPills}
    <details class="raw-details"><summary>查看接口原始数据</summary><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></details>`;
  $('quoteEmpty').classList.add('hidden');
  $('quoteResult').classList.remove('hidden');
}

/* ---------------------------------------------------------------- knowledge */
function renderKbHealth(weknora) {
  const configured = Boolean(weknora && weknora.configured);
  const required = Boolean(weknora && weknora.required);
  const status = $('kbStatus');
  if (configured) {
    status.className = 'pill success';
    status.innerHTML = `${ic('check')}WeKnora 已配置`;
    $('kbHealth').innerHTML = `
      <div class="health-title"><span class="dot"></span>WeKnora 混合检索</div>
      <p>知识库 <code>${escapeHtml(weknora.kb_id || '已配置')}</code>；业务问答强制走唯一知识底座。</p>
      <p class="health-foot">发布标准后会自动同步到该知识库。</p>`;
  } else if (required) {
    status.className = 'pill danger';
    status.innerHTML = `${ic('alert')}WeKnora 未配置`;
    $('kbHealth').innerHTML = `
      <div class="health-title"><span class="dot" style="background:#f04438;box-shadow:0 0 0 3px rgba(240,68,56,.14)"></span>知识底座不可用</div>
      <p>生产配置要求 WeKnora 为唯一知识底座，当前未连接，发布与问答会拒绝请求。</p>
      <p class="health-foot">请配置 P0_WEKNORA_BASE_URL / API_KEY / KB_ID。</p>`;
  } else {
    status.className = 'pill warn';
    status.innerHTML = `${ic('alert')}本地演示检索`;
    $('kbHealth').innerHTML = `
      <div class="health-title"><span class="dot" style="background:#f79009;box-shadow:0 0 0 3px rgba(247,144,9,.14)"></span>本地关键词回退</div>
      <p>WeKnora 未连接，当前使用本地关键词检索演示完整链路；发布只写本地审计副本。</p>
      <p class="health-foot">生产环境应配置 WeKnora 后再演示发布与检索。</p>`;
  }
}
async function loadKbHealth() {
  try {
    const data = await jsonFetch('api/v1/system/status');
    state.kbHealth = data.weknora || {};
    renderKbHealth(state.kbHealth);
  } catch (error) {
    $('kbStatus').className = 'pill danger';
    $('kbStatus').innerHTML = `${ic('alert')}状态未知`;
    $('kbHealth').innerHTML = `<div class="banner danger">${ic('alert')}<span>无法读取系统状态：${escapeHtml(error.message)}</span></div>`;
  }
}
function renderFeedbackStats() {
  const card = $('feedbackStatsCard');
  const data = state.feedback;
  if (!data) {
    card.classList.add('hidden');
    return;
  }
  card.classList.remove('hidden');
  const totals = data.totals || {};
  $('feedbackTotal').textContent = totals.total ?? 0;
  $('feedbackStats').innerHTML = [
    `<div class="feedback-stat"><span>有帮助</span><b>${escapeHtml(totals.helpful ?? 0)}</b></div>`,
    `<div class="feedback-stat"><span>没帮助</span><b>${escapeHtml(totals.not_helpful ?? 0)}</b></div>`,
  ].join('');
  const reasons = data.reasons || [];
  $('feedbackReasons').innerHTML = reasons.length
    ? reasons.map((item) => `<span class="pill warn">${escapeHtml(item.reason)} ${escapeHtml(item.count)}</span>`).join('')
    : '<span class="muted">还没有「没帮助」原因记录</span>';
}
async function loadFeedbackStats() {
  if (!can('feedback:read')) return;
  try {
    state.feedback = await jsonFetch('api/v1/kb/feedback?limit=5');
  } catch (error) {
    state.feedback = null;
  }
  renderFeedbackStats();
}
async function loadKbStandards() {
  try {
    const data = await jsonFetch('api/v1/standards');
    const published = (data.standards || []).filter((standard) => standard.published);
    const select = $('kbStdSelect');
    const current = select.value;
    select.innerHTML = '<option value="">全部已发布标准</option>' + published.map((standard) => {
      const label = `${standard.standard_no || standard.name} · ${standard.items} 项`;
      return `<option value="${escapeHtml(standard.id)}">${escapeHtml(label)}</option>`;
    }).join('');
    if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
  } catch (error) {
    showError(error);
  }
}
function renderAnswer(data) {
  const found = data.found !== false;
  $('answerBox').classList.remove('loading');
  $('answerBox').classList.toggle('refusal', !found);
  $('answerBox').textContent = data.answer || '未找到可核实依据。';
  $('copyAnswerBtn').classList.toggle('hidden', !found);
  $('citations').innerHTML = (data.citations || []).map((citation, index) => {
    const jumpable = Boolean(citation.standard_id);
    const tag = jumpable ? 'button' : 'div';
    const attrs = jumpable
      ? ` type="button" data-citation-standard="${escapeHtml(citation.standard_id)}" data-citation-clause="${escapeHtml(citation.clause || '')}"`
      : '';
    return `<${tag} class="citation"${attrs}>
      <span class="citation-no">${index + 1}</span>
      <div class="citation-body">
        <div class="citation-head"><b>${escapeHtml(citation.standard_no || citation.standard_name || '')}</b><span>条款 ${escapeHtml(citation.clause || '—')}</span><span>第 ${escapeHtml(citation.page ?? '—')} 页</span><span class="pill neutral">${escapeHtml(citation.kind_label || '知识片段')}</span>${jumpable ? '<span class="citation-jump">查看条款 →</span>' : ''}</div>
        <div class="citation-quote">${escapeHtml(citation.quote || '')}</div>
      </div>
    </${tag}>`;
  }).join('');
  const retrieval = data.retrieval || {};
  const mode = retrieval.mode || '';
  const modeLabel = mode === 'weknora' ? 'WeKnora 混合检索' : mode.indexOf('keyword') === 0 ? '本地关键词检索' : mode === 'local-fallback' ? '本地向量回退' : (mode || '检索');
  const meta = [`<span class="pill info">${escapeHtml(modeLabel)}</span>`];
  if (retrieval.top_score !== undefined && retrieval.top_score !== null) meta.push(`<span class="pill neutral">相关度 ${escapeHtml(Number(retrieval.top_score).toFixed(3))}</span>`);
  if (retrieval.kb_id) meta.push(`<span class="pill neutral">知识库 ${escapeHtml(retrieval.kb_id)}</span>`);
  if (retrieval.fallback_reason) meta.push(`<span class="pill warn">本地回退</span>`);
  $('retrievalMeta').innerHTML = meta.join('');
  const feedback = $('answerFeedback');
  if (!found) {
    feedback.classList.add('hidden');
    feedback.innerHTML = '';
  } else {
    feedback.classList.remove('hidden');
    feedback.innerHTML = `<span>这个回答有帮助吗？</span>
      <button type="button" class="btn btn-secondary btn-sm" data-answer-feedback="helpful">${ic('check')}有帮助</button>
      <button type="button" class="btn btn-secondary btn-sm" data-answer-feedback="not_helpful">${ic('alert')}没帮助</button>
      <span class="muted" id="answerFeedbackStatus"></span>`;
  }
}
async function askQuestion() {
  const question = $('question').value.trim();
  if (!question) {
    toast('请输入问题后再检索', 'info');
    return;
  }
  busy($('askBtn'), true, '检索中…');
  $('answerBox').classList.remove('hidden', 'refusal');
  $('answerBox').classList.add('loading');
  $('answerBox').textContent = '正在检索已发布标准…';
  $('citations').innerHTML = '';
  $('retrievalMeta').innerHTML = '';
  $('copyAnswerBtn').classList.add('hidden');
  try {
    const data = await jsonFetch('api/v1/standards/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, standard_id: $('kbStdSelect').value || null }),
    });
    state.lastAnswer = {
      question,
      answer: data.answer || '',
      retrieval_mode: (data.retrieval || {}).mode || '',
      citations: data.citations || [],
    };
    renderAnswer(data);
  } catch (error) {
    $('answerBox').classList.remove('loading');
    $('answerBox').classList.add('refusal');
    $('answerBox').textContent = `提问失败：${error.message}`;
    toast('提问失败', 'error');
  } finally {
    busy($('askBtn'), false);
  }
}

/* ---------------------------------------------------------------- admin */
function renderUserKpis() {
  const users = state.users;
  const active = users.filter((user) => user.active).length;
  const restricted = users.filter((user) => user.restricted).length;
  $('userKpis').innerHTML = [
    kpi(users.length, '账号总数', 'info', 'users'),
    kpi(active, '启用账号', 'success', 'check'),
    kpi(restricted, '项目受限', 'warn', 'folder'),
    kpi(4, '角色数量', 'violet', 'shield'),
  ].join('');
}
function renderRoleCards() {
  const counts = {};
  state.users.forEach((user) => { counts[user.role] = (counts[user.role] || 0) + 1; });
  $('roleCards').innerHTML = ['admin', 'reviewer', 'sales', 'viewer'].map((role) => `
    <button type="button" class="role-card ${state.userRoleFilter === role ? 'active' : ''}" data-role="${role}">
      <div class="role-top"><b>${ROLE_NAMES[role]}</b><span class="role-count">${counts[role] || 0}</span></div>
      <p>${ROLE_DESCRIPTIONS[role]}</p>
    </button>`).join('');
}
function renderUsers() {
  const query = ($('userSearch').value || '').trim().toLowerCase();
  const users = state.users.filter((user) => {
    if (state.userRoleFilter && user.role !== state.userRoleFilter) return false;
    if (!query) return true;
    return `${user.username || ''} ${user.display_name || ''} ${user.department || ''}`.toLowerCase().includes(query);
  });
  if (!users.length) {
    $('userBody').innerHTML = '<tr><td colspan="6"><div class="empty compact"><b>没有匹配的账号</b><p>调整搜索词或角色筛选后重试。</p></div></td></tr>';
    return;
  }
  $('userBody').innerHTML = users.map((user) => {
    const username = escapeHtml(user.username || '');
    const name = user.display_name || user.username || '未命名';
    const options = ['admin', 'reviewer', 'sales', 'viewer'].map((role) => `<option value="${role}" ${user.role === role ? 'selected' : ''}>${ROLE_NAMES[role]}</option>`).join('');
    const projects = (user.projects || []).map((project) => `<span class="chip">${project === '*' ? '全部项目' : escapeHtml(project)}</span>`).join('') || '<span class="muted">不限制</span>';
    const status = user.active ? '<span class="pill success">启用</span>' : '<span class="pill neutral">停用</span>';
    return `<tr>
      <td><div class="user-cell"><span class="avatar">${escapeHtml(initials(name))}</span><div><span class="u-name">${escapeHtml(name)}</span><span class="u-login">@${username}${user.restricted ? ' · 项目受限' : ''}</span></div></div></td>
      <td><select class="role-select" data-role-user="${username}">${options}</select></td>
      <td>${escapeHtml(user.department || '—')}</td>
      <td>${projects}</td>
      <td>${status}${user.locked ? '<span class="pill danger">锁定</span>' : ''}</td>
      <td class="actions">
        <button type="button" class="btn btn-secondary btn-sm" data-act="projects" data-user="${username}" data-projects="${escapeHtml((user.projects || []).join(','))}">${ic('folder')}项目</button>
        <button type="button" class="btn btn-secondary btn-sm" data-act="reset" data-user="${username}">${ic('key')}重置</button>
        <button type="button" class="btn ${user.active ? 'btn-danger' : 'btn-secondary'} btn-sm" data-act="toggle" data-user="${username}" data-active="${user.active ? '1' : '0'}">${user.active ? '停用' : '启用'}</button>
      </td>
    </tr>`;
  }).join('');
}
function populateAuditUsers() {
  const select = $('auUser');
  const current = select.value;
  select.innerHTML = '<option value="">全部用户</option>' + state.users.map((user) => `<option value="${escapeHtml(user.username)}">${escapeHtml(user.display_name || user.username)}（${escapeHtml(user.username)}）</option>`).join('');
  if (Array.from(select.options).some((option) => option.value === current)) select.value = current;
}
async function loadAdmin() {
  const data = await jsonFetch('api/v1/auth/users');
  state.users = data.users || [];
  populateAuditUsers();
  renderUserKpis();
  renderRoleCards();
  renderUsers();
  if (state.adminView === 'audit') await loadAudit(false);
}
async function patchUser(username, body, successMessage) {
  try {
    await jsonFetch(`api/v1/auth/users/${encodeURIComponent(username)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast(successMessage);
    await loadAdmin();
  } catch (error) {
    toast(error.message, 'error');
  }
}
function openProjectModal(username, projects) {
  state.projectUser = username;
  $('projectSub').textContent = `设置 ${username} 可访问的项目；留空表示不限制。`;
  $('projectInput').value = projects || '';
  $('projectStatus').textContent = '';
  const known = Array.from(new Set([
    ...state.standards.map((standard) => standard.project),
    ...state.users.flatMap((user) => user.projects || []),
  ].filter((project) => project && project !== '*'))).slice(0, 8);
  $('projectPresets').innerHTML = known.map((project) => `<button type="button" data-project="${escapeHtml(project)}">${escapeHtml(project)}</button>`).join('');
  openModal('modalProject');
}
function openResetModal(username) {
  state.resetUser = username;
  $('resetSub').textContent = `为 ${username} 生成一次性临时密码。`;
  $('resetResult').classList.add('hidden');
  $('resetNotice').classList.remove('hidden');
  $('resetStatus').textContent = '';
  $('resetPasswordOut').textContent = '—';
  $('resetGenerate').disabled = false;
  $('resetGenerate').innerHTML = `${ic('key')}生成临时密码`;
  openModal('modalReset');
}
function auditFilters() {
  const filters = {};
  if ($('auUser').value) filters.user = $('auUser').value;
  if ($('auEvent').value) filters.event = $('auEvent').value;
  if ($('auStatus').value) filters.status = $('auStatus').value;
  if ($('auFrom').value) filters.date_from = $('auFrom').value;
  if ($('auTo').value) filters.date_to = $('auTo').value;
  return filters;
}
function statusMeta(status) {
  const code = Number(status);
  if (Number.isFinite(code) && code >= 200 && code < 300) return { tone: 'success', text: `${code} 成功` };
  if (code === 401) return { tone: 'warn', text: '401 未认证' };
  if (code === 403) return { tone: 'danger', text: '403 无权限' };
  if (code === 423) return { tone: 'danger', text: '423 锁定' };
  if (code === 429) return { tone: 'warn', text: '429 限流' };
  if (code >= 500) return { tone: 'danger', text: `${code} 服务错误` };
  if (code >= 400) return { tone: 'warn', text: `${code} 请求异常` };
  return { tone: 'neutral', text: status === undefined || status === null || status === '' ? '—' : String(status) };
}
function auditEventLabel(entry) {
  if (entry.event && EVENT_LABELS[entry.event]) return EVENT_LABELS[entry.event];
  if (entry.event) return entry.event;
  return '接口访问';
}
function renderAuditChart(events) {
  const days = [];
  const now = new Date();
  for (let offset = 6; offset >= 0; offset -= 1) {
    const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset);
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    days.push({ key, label: `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`, ok: 0, bad: 0 });
  }
  const byKey = new Map(days.map((day) => [day.key, day]));
  events.forEach((entry) => {
    const date = new Date(entry.at);
    if (Number.isNaN(date.getTime())) return;
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    const day = byKey.get(key);
    if (!day) return;
    if (Number(entry.status) >= 400) day.bad += 1;
    else day.ok += 1;
  });
  if (!events.length) {
    $('auditChart').innerHTML = '<div class="empty compact" style="width:100%"><b>暂无审计活动</b><p>调整筛选条件后重试。</p></div>';
    return;
  }
  const max = Math.max(1, ...days.map((day) => day.ok + day.bad));
  $('auditChart').innerHTML = days.map((day) => {
    const okHeight = Math.round((day.ok / max) * 120);
    const badHeight = Math.round((day.bad / max) * 120);
    return `<div class="chart-col" title="${escapeHtml(day.key)}：成功 ${day.ok} · 异常 ${day.bad}">
      <div class="chart-stack">${day.bad ? `<div class="chart-bar bad" style="height:${badHeight}px"></div>` : ''}${day.ok ? `<div class="chart-bar" style="height:${okHeight}px"></div>` : ''}</div>
      <span class="chart-label">${escapeHtml(day.label)}</span>
    </div>`;
  }).join('');
}
function renderAudit() {
  const all = state.audit.all;
  const failures = all.filter((entry) => Number(entry.status) >= 400).length;
  const users = new Set(all.map((entry) => entry.user).filter(Boolean)).size;
  const durations = all.map((entry) => Number(entry.ms)).filter(Number.isFinite);
  const average = durations.length ? (durations.reduce((sum, value) => sum + value, 0) / durations.length).toFixed(1) : '—';
  $('auditKpis').innerHTML = [
    kpi(state.audit.total, '事件总数', 'info', 'activity'),
    kpi(failures, '异常请求', 'danger', 'alert'),
    kpi(users, '活跃用户', 'violet', 'users'),
    kpi(average, '平均耗时', 'neutral', 'clock', '毫秒'),
  ].join('');
  renderAuditChart(all);
  const events = state.audit.events;
  $('auditBody').innerHTML = events.map((entry, index) => {
    const status = statusMeta(entry.status);
    const role = ROLE_NAMES[entry.role] || entry.role || '—';
    return `<tr data-audit-index="${index}">
      <td class="muted">${escapeHtml(formatTime(entry.at))}</td>
      <td><span class="audit-user">${escapeHtml(entry.user || '—')}</span></td>
      <td class="muted">${escapeHtml(role)}</td>
      <td><span class="cell-main">${escapeHtml(auditEventLabel(entry))}</span><span class="cell-sub ellipsis path-cell">${escapeHtml(entry.action || entry.path || '')}</span></td>
      <td><span class="pill ${status.tone}">${escapeHtml(status.text)}</span></td>
      <td class="muted">${entry.ms !== undefined && entry.ms !== null ? `${escapeHtml(entry.ms)} ms` : '—'}</td>
      <td class="muted">${escapeHtml(entry.ip || '—')}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="7"><div class="empty compact"><b>没有匹配的审计记录</b><p>调整筛选条件后重试。</p></div></td></tr>';
  const total = state.audit.total;
  const offset = state.audit.offset;
  const limit = state.audit.limit;
  const shown = events.length;
  const pages = Math.max(1, Math.ceil(total / limit));
  $('auditMeta').textContent = total ? `共 ${total} 条，显示第 ${offset + 1}–${Math.min(offset + shown, total)} 条` : '共 0 条';
  $('auditPage').textContent = `${Math.floor(offset / limit) + 1} / ${pages}`;
  $('auditPrev').disabled = offset <= 0;
  $('auditNext').disabled = offset + limit >= total;
}
async function loadAudit(resetPage) {
  const filters = auditFilters();
  const limit = Number($('auLimit').value) || 50;
  if (resetPage) state.audit.offset = 0;
  const pageQuery = new URLSearchParams(filters);
  pageQuery.set('limit', String(limit));
  pageQuery.set('offset', String(state.audit.offset));
  const chartQuery = new URLSearchParams(filters);
  chartQuery.set('limit', '500');
  chartQuery.set('offset', '0');
  try {
    const [page, all] = await Promise.all([
      jsonFetch(`api/v1/audit/events?${pageQuery.toString()}`),
      jsonFetch(`api/v1/audit/events?${chartQuery.toString()}`),
    ]);
    state.audit = {
      events: page.events || [],
      total: page.total || 0,
      limit,
      offset: page.offset || 0,
      all: all.events || [],
    };
    renderAudit();
  } catch (error) {
    showError(error);
  }
}
function openAuditDetail(index) {
  const entry = state.audit.events[index];
  if (!entry) return;
  const rows = [
    ['时间', formatTime(entry.at, true)],
    ['用户', entry.user || '—'],
    ['角色', ROLE_NAMES[entry.role] || entry.role || '—'],
    ['事件', auditEventLabel(entry)],
    ['方法', entry.method || '—'],
    ['路径', entry.path || '—'],
    ['动作', entry.action || '—'],
    ['状态', statusMeta(entry.status).text],
    ['耗时', entry.ms !== undefined && entry.ms !== null ? `${entry.ms} ms` : '—'],
    ['IP', entry.ip || '—'],
  ];
  $('auditDetailBody').innerHTML = `<dl class="audit-detail-grid">${rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join('')}</dl>`;
  $('auditDetailRaw').textContent = JSON.stringify(entry, null, 2);
  openModal('modalAuditDetail');
}
/* ---------------------------------------------------------------- rule config */
const CONFIG_SOURCE_NAMES = {
  sample_name: '样品名称',
  item_name: '项目 / 检验项目',
  standard: '标准',
  department: '检验科室',
  unit: '单位',
  quantity: '数量',
  unit_price: '含税单价',
  has_discount: '是否有折扣',
  discount: '折扣',
  note: '备注',
};
function splitList(value) {
  return String(value || '').split(/[,，\n]+/).map((item) => item.trim()).filter(Boolean);
}
function configTime(value) {
  return value ? formatTime(value, true) : '尚未保存，当前为默认规则';
}
function renderAdminConfig() {
  const wrapper = state.config || {};
  const config = wrapper.config || {};
  const report = config.report || { rules: [] };
  const standard = config.standard || { section_aliases: {}, fields: [] };
  const quote = config.quote || { headers: {}, unit_map: {}, crm_columns: [] };
  const business = config.business || {};
  const businessRush = business.rush || {};
  const businessIntermediary = business.intermediary || {};

  $('configMeta').textContent = `配置版本 ${config.revision ?? 0} · ${configTime(config.updated_at)}${config.updated_by ? ` · ${config.updated_by}` : ''}`;
  if (wrapper.warning) {
    $('configWarning').classList.remove('hidden');
    $('configWarning').innerHTML = `${ic('alert')}<span>${escapeHtml(wrapper.warning)}</span>`;
  } else {
    $('configWarning').classList.add('hidden');
  }

  $('cfgMeanTolerance').value = report.mean_tolerance ?? 0.05;
  $('cfgExpectedNotices').value = report.expected_notices ?? 8;
  $('cfgRuleBody').innerHTML = (report.rules || []).map((rule) => `
    <tr data-rule-id="${escapeHtml(rule.id)}">
      <td><span class="finding-rule">${escapeHtml(rule.id)}</span></td>
      <td><input type="checkbox" data-rule-field="enabled" ${rule.enabled ? 'checked' : ''} aria-label="${escapeHtml(rule.id)} 启用"></td>
      <td><select data-rule-field="severity" aria-label="${escapeHtml(rule.id)} 严重级别">${Object.entries(SEVERITY_NAMES).map(([value, label]) => `<option value="${value}" ${rule.severity === value ? 'selected' : ''}>${label}</option>`).join('')}</select></td>
      <td><input type="text" data-rule-field="title" value="${escapeHtml(rule.title)}" maxlength="100" aria-label="${escapeHtml(rule.id)} 名称"></td>
      <td><input type="text" data-rule-field="description" value="${escapeHtml(rule.description)}" maxlength="500" aria-label="${escapeHtml(rule.id)} 说明"></td>
    </tr>`).join('');

  $('cfgStdAliasTech').value = (standard.section_aliases?.['技术要求'] || []).join(', ');
  $('cfgStdAliasMethod').value = (standard.section_aliases?.['试验方法'] || []).join(', ');
  $('cfgStdFieldBody').innerHTML = (standard.fields || []).map((field) => `
    <tr data-field-key="${escapeHtml(field.key)}">
      <td><span class="cell-main">${escapeHtml(field.key)}</span></td>
      <td><input type="text" data-field-prop="label" value="${escapeHtml(field.label)}" maxlength="40" aria-label="${escapeHtml(field.key)} 显示名"></td>
      <td><input type="checkbox" data-field-prop="required" ${field.required ? 'checked' : ''} aria-label="${escapeHtml(field.key)} 必填"></td>
      <td><input type="checkbox" data-field-prop="export" ${field.export ? 'checked' : ''} aria-label="${escapeHtml(field.key)} 导出"></td>
    </tr>`).join('');

  $('cfgQuoteDiscount').value = quote.default_discount ?? 60;
  $('cfgQuoteProductTax').value = Math.round((quote.product_tax_rate ?? 0.16) * 10000) / 100;
  $('cfgQuoteServiceTax').value = Math.round((quote.service_tax_rate ?? 0.06) * 10000) / 100;
  $('cfgQuoteHeadersItem').value = (quote.headers?.item || []).join(', ');
  $('cfgQuoteHeadersPrice').value = (quote.headers?.price || []).join(', ');
  $('cfgQuoteHeadersQuantity').value = (quote.headers?.quantity || []).join(', ');
  $('cfgQuoteHeadersUnit').value = (quote.headers?.unit || []).join(', ');
  $('cfgQuoteHeadersSample').value = (quote.headers?.sample || []).join(', ');
  $('cfgQuoteUnitMap').value = Object.entries(quote.unit_map || {}).map(([source, target]) => `${source}=${target}`).join('\n');
  $('cfgQuoteColumnBody').innerHTML = (quote.crm_columns || []).map((column) => `
    <tr data-column-source="${escapeHtml(column.source)}">
      <td><span class="cell-main">${escapeHtml(CONFIG_SOURCE_NAMES[column.source] || column.source)}</span><span class="cell-sub">${escapeHtml(column.source)}</span></td>
      <td><input type="text" data-column-prop="label" value="${escapeHtml(column.label)}" maxlength="40" aria-label="${escapeHtml(column.label)} 列名"></td>
      <td><input type="checkbox" data-column-prop="enabled" ${column.enabled ? 'checked' : ''} aria-label="${escapeHtml(column.label)} 启用"></td>
    </tr>`).join('');

  $('cfgBizDiscountTiers').value = (business.discount_tiers || []).map((tier) => `${tier.min_amount}=${tier.discount}`).join('\n');
  $('cfgBizRushEnabled').checked = Boolean(businessRush.enabled);
  $('cfgBizRushFee').value = businessRush.fee_rate ?? 0;
  $('cfgBizRushLead').value = businessRush.lead_days ?? 0;
  $('cfgBizIntermediaryEnabled').checked = Boolean(businessIntermediary.enabled);
  $('cfgBizIntermediaryBlock').checked = Boolean(businessIntermediary.block);
  $('cfgBizIntermediaryKeywords').value = (businessIntermediary.keywords || []).join(', ');
  $('cfgBizBlacklist').value = (business.blacklist?.companies || []).join('\n');
  $('cfgBizWhitelist').value = (business.whitelist?.companies || []).join('\n');

  Object.entries(ADMIN_CONFIG_SECTIONS).forEach(([section, meta]) => $(meta.id).classList.toggle('hidden', !canConfig(section)));
}
async function loadAdminConfig() {
  state.config = await jsonFetch('api/v1/admin/config');
  renderAdminConfig();
}
function collectReportConfig() {
  const rules = $$('#cfgRuleBody tr[data-rule-id]').map((row) => ({
    id: row.dataset.ruleId,
    enabled: row.querySelector('[data-rule-field="enabled"]').checked,
    severity: row.querySelector('[data-rule-field="severity"]').value,
    title: row.querySelector('[data-rule-field="title"]').value.trim(),
    description: row.querySelector('[data-rule-field="description"]').value.trim(),
  }));
  return {
    mean_tolerance: Number($('cfgMeanTolerance').value),
    expected_notices: Number($('cfgExpectedNotices').value),
    rules,
  };
}
function collectStandardConfig() {
  const fields = $$('#cfgStdFieldBody tr[data-field-key]').map((row) => ({
    key: row.dataset.fieldKey,
    label: row.querySelector('[data-field-prop="label"]').value.trim(),
    required: row.querySelector('[data-field-prop="required"]').checked,
    export: row.querySelector('[data-field-prop="export"]').checked,
  }));
  return {
    section_aliases: {
      '技术要求': splitList($('cfgStdAliasTech').value),
      '试验方法': splitList($('cfgStdAliasMethod').value),
    },
    fields,
  };
}
function parseUnitMap(value) {
  const unitMap = {};
  String(value || '').split('\n').forEach((line) => {
    const text = line.trim();
    if (!text) return;
    const parts = text.split(/[=＝]/);
    if (parts.length !== 2) throw new Error(`单位映射行格式不正确：${text}（应为「源=目标」）`);
    const source = parts[0].trim();
    const target = parts[1].trim();
    if (!source || !target) throw new Error(`单位映射行格式不正确：${text}`);
    unitMap[source] = target;
  });
  return unitMap;
}
function collectQuoteConfig() {
  const columns = $$('#cfgQuoteColumnBody tr[data-column-source]').map((row) => ({
    source: row.dataset.columnSource,
    label: row.querySelector('[data-column-prop="label"]').value.trim(),
    enabled: row.querySelector('[data-column-prop="enabled"]').checked,
  }));
  return {
    default_discount: Number($('cfgQuoteDiscount').value),
    product_tax_rate: Number($('cfgQuoteProductTax').value) / 100,
    service_tax_rate: Number($('cfgQuoteServiceTax').value) / 100,
    headers: {
      item: splitList($('cfgQuoteHeadersItem').value),
      price: splitList($('cfgQuoteHeadersPrice').value),
      quantity: splitList($('cfgQuoteHeadersQuantity').value),
      unit: splitList($('cfgQuoteHeadersUnit').value),
      sample: splitList($('cfgQuoteHeadersSample').value),
    },
    unit_map: parseUnitMap($('cfgQuoteUnitMap').value),
    crm_columns: columns,
  };
}
function parseDiscountTiers(value) {
  return String(value || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split(/[=＝]/);
    const minAmount = Number(parts[0].trim());
    const discount = Number(parts[1]?.trim());
    if (parts.length !== 2 || !Number.isFinite(minAmount) || !Number.isFinite(discount)) {
      throw new Error(`梯度折扣行格式不正确：${line}（应为「最低金额=折扣%」）`);
    }
    return { min_amount: minAmount, discount };
  });
}
function collectBusinessConfig() {
  return {
    discount_tiers: parseDiscountTiers($('cfgBizDiscountTiers').value),
    rush: {
      enabled: $('cfgBizRushEnabled').checked,
      fee_rate: Number($('cfgBizRushFee').value),
      lead_days: Number($('cfgBizRushLead').value),
    },
    intermediary: {
      enabled: $('cfgBizIntermediaryEnabled').checked,
      block: $('cfgBizIntermediaryBlock').checked,
      keywords: splitList($('cfgBizIntermediaryKeywords').value),
    },
    blacklist: { companies: splitList($('cfgBizBlacklist').value) },
    whitelist: { companies: splitList($('cfgBizWhitelist').value) },
  };
}
async function saveConfigSection(section, button, collector) {
  let body;
  try {
    body = collector();
  } catch (error) {
    showError(error);
    return;
  }
  busy(button, true, '保存中…');
  try {
    state.config = await jsonFetch(`api/v1/admin/config/${section}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    renderAdminConfig();
    await loadModuleConfig();
    toast('规则配置已保存，对新的请求立即生效');
  } catch (error) {
    showError(error);
  } finally {
    busy(button, false);
  }
}
async function resetConfigSection(section, label) {
  const confirmed = await confirmDialog({
    title: `恢复默认${label}？`,
    message: '当前自定义值会被默认规则覆盖；已保存的业务数据不受影响。',
    confirmText: '恢复默认',
    tone: 'danger',
  });
  if (!confirmed) return;
  try {
    state.config = await jsonFetch(`api/v1/admin/config/${section}/reset`, { method: 'POST' });
    renderAdminConfig();
    await loadModuleConfig();
    toast(`已恢复默认${label}`);
  } catch (error) {
    showError(error);
  }
}
const ADMIN_CONFIG_SECTIONS = {
  report: { id: 'config-section-report', label: '报告审核规则' },
  standard: { id: 'config-section-standard', label: '标准解析规则字段' },
  quote: { id: 'config-section-quote', label: '报价助手规则字段' },
  business: { id: 'config-section-business', label: '业务规则' },
};
let adminConfigLoad = Promise.resolve();
let configFocusTimer = null;
function adminConfigSection(section) {
  return ADMIN_CONFIG_SECTIONS[section] || null;
}
function syncAdminLocation(view, section = '') {
  const nextParams = new URLSearchParams(location.search);
  nextParams.set('tab', 'admin');
  if (view) nextParams.set('view', view);
  if (section) nextParams.set('section', section);
  else nextParams.delete('section');
  history.replaceState(null, '', `${location.pathname}?${nextParams.toString()}`);
}
function focusAdminConfigSection(section) {
  const meta = adminConfigSection(section);
  if (!meta || !canConfig(section)) return false;
  const target = $(meta.id);
  if (!target) return false;
  target.classList.remove('config-target');
  void target.offsetWidth;
  target.classList.add('config-target');
  target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  window.clearTimeout(configFocusTimer);
  configFocusTimer = window.setTimeout(() => target.classList.remove('config-target'), 3200);
  return true;
}
function switchAdminView(view, section = '') {
  const canManageUsers = can('user:manage');
  if (!canManageUsers && view !== 'config') view = 'config';
  state.adminView = view;
  $$('#adminTabs button').forEach((button) => {
    button.classList.toggle('hidden', !canManageUsers && button.dataset.adminView !== 'config');
    button.classList.toggle('active', button.dataset.adminView === view);
  });
  $('adminUsersView').classList.toggle('hidden', view !== 'users');
  $('adminAuditView').classList.toggle('hidden', view !== 'audit');
  $('adminConfigView').classList.toggle('hidden', view !== 'config');
  if (view === 'audit') loadAudit(true);
  if (view === 'config') adminConfigLoad = loadAdminConfig().catch(showError);
  syncAdminLocation(view, view === 'config' ? section : '');
}
async function openAdminConfig(section = '') {
  if (!canConfig(section)) return;
  closeModal('modalStdConfig');
  closeModal('modalQuoteConfig');
  state.adminView = 'config';
  switchTab('admin');
  syncAdminLocation('config', section);
  const meta = adminConfigSection(section);
  if (!meta) return;
  await adminConfigLoad;
  // 等两帧让配置表格完成布局，再滚动到目标分区，避免落点因表格高度变化而偏移。
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (focusAdminConfigSection(section)) toast(`已定位到「${meta.label}」`, 'info');
  }));
}

/* ---------------------------------------------------------------- bind */
function bindEvents() {
  $$('.nav-item').forEach((item) => item.addEventListener('click', () => switchTab(item.dataset.tab)));
  document.addEventListener('click', (event) => {
    const configLink = event.target.closest('.js-open-admin-config');
    if (configLink) openAdminConfig(configLink.dataset.configSection || '');
  });
  $('stdConfigOpen').addEventListener('click', () => openModal('modalStdConfig'));
  $('quoteConfigOpen').addEventListener('click', () => openModal('modalQuoteConfig'));
  $('navToggle').addEventListener('click', () => {
    $('sidebar').classList.add('open');
    $('navBackdrop').classList.add('show');
  });
  $('navBackdrop').addEventListener('click', closeSidebar);

  setupDropzone('reportDrop', 'reportFile', { title: '点击选择，或把报告拖到这里', sub: '只看结构、字段与标记，不上传外部服务' });
  setupDropzone('stdDrop', 'stdFile', { title: '选择标准文档', sub: 'PDF / Word（.docx）/ 图片；数字版与扫描件均可' });
  setupDropzone('quoteDrop', 'quoteFile', { title: '选择报价输入', sub: '.xlsx / .xls / .pdf / .png / .jpg' });
  setupDropzone('templateDrop', 'quoteTemplate', { title: '选择销售报价表模板', sub: '不提供时只生成 CRM 草稿' });

  $('auditBtn').addEventListener('click', async () => {
    const files = Array.from($('reportFile').files || []);
    if (!files.length) { toast('请先选择 .docx 报告', 'info'); return; }
    const project = ($('reportProject').value || '').trim();
    busy($('auditBtn'), true, files.length > 1 ? '批量审核中…' : '审核中…');
    $('reportStatus').textContent = '';
    try {
      if (files.length > 1) {
        const form = new FormData();
        files.forEach((file) => form.append('files', file));
        if (project) form.append('project', project);
        let data = await jsonFetch('api/v1/reports/batch', { method: 'POST', body: form });
        state.currentReport = null;
        if (data.task_id) {
          $('reportStatus').textContent = '已提交后台审核，等待结果…';
          data = await waitForReportTask(data.task_id);
        }
        renderReportBatch(data);
        $('reportStatus').textContent = `完成：${data.summary.stored} 份成功，${data.summary.failed} 份失败`;
        toast(`批量审核完成：${data.summary.stored} 份`);
      } else {
        const form = new FormData();
        form.append('file', files[0]);
        if (project) form.append('project', project);
        const data = await jsonFetch('api/v1/reports/audit', { method: 'POST', body: form });
        state.currentReport = {
          id: data.report_id,
          project: data.project,
          created_at: data.created_at,
          created_by: (state.me && state.me.username) || '',
          file_name: data.file,
          summary: data.summary,
        };
        renderReport(data);
        $('reportStatus').textContent = `完成：${data.summary.fail} 项需要处理`;
        toast('报告审核完成');
      }
      if (can('report:read')) {
        state.reportPage.offset = 0;
        await loadReports();
      }
    } catch (error) {
      $('reportStatus').textContent = '审核失败';
      showError(error);
    } finally {
      busy($('auditBtn'), false);
    }
  });
  $('reportHistory').addEventListener('click', (event) => {
    const item = event.target.closest('[data-report-id]');
    if (!item) return;
    loadReport(item.dataset.reportId).catch(showError);
  });
  $('reportHistorySearch').addEventListener('click', () => {
    state.reportPage.query = $('reportHistoryQuery').value.trim();
    state.reportPage.offset = 0;
    loadReports().catch(showError);
  });
  $('reportHistoryQuery').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    state.reportPage.query = $('reportHistoryQuery').value.trim();
    state.reportPage.offset = 0;
    loadReports().catch(showError);
  });
  $('reportHistoryPrev').addEventListener('click', () => {
    state.reportPage.offset = Math.max(0, state.reportPage.offset - state.reportPage.limit);
    loadReports().catch(showError);
  });
  $('reportHistoryNext').addEventListener('click', () => {
    state.reportPage.offset += state.reportPage.limit;
    loadReports().catch(showError);
  });
  $('reportResult').addEventListener('click', (event) => {
    const exportButton = event.target.closest('[data-report-export]');
    if (exportButton) {
      window.location = `api/v1/reports/${encodeURIComponent(exportButton.dataset.reportExport)}/export.xlsx`;
      return;
    }
    const sourceButton = event.target.closest('[data-report-source]');
    if (sourceButton) {
      window.location = `api/v1/reports/${encodeURIComponent(sourceButton.dataset.reportSource)}/source.docx`;
      return;
    }
    const batchButton = event.target.closest('[data-batch-export]');
    if (batchButton) {
      window.location = `api/v1/reports/batch/${encodeURIComponent(batchButton.dataset.batchExport)}/export.xlsx`;
      return;
    }
    const reviewButton = event.target.closest('[data-review-status]');
    if (reviewButton) {
      reviewFinding(reviewButton.dataset.reviewReport, reviewButton.dataset.reviewRule, reviewButton.dataset.reviewStatus).catch(showError);
      return;
    }
    const item = event.target.closest('[data-report-id]');
    if (!item) return;
    loadReport(item.dataset.reportId).catch(showError);
  });
  $('reportResult').addEventListener('change', (event) => {
    if (event.target.id !== 'findingSeverityFilter') return;
    state.reportSeverityFilter = event.target.value;
    if (state.currentReportData) renderReport(state.currentReportData);
  });
  $('recordsSample').addEventListener('click', () => {
    $('records').value = SAMPLE_RECORDS;
    toast('已载入示例记录', 'info');
  });
  $('validateBtn').addEventListener('click', async () => {
    busy($('validateBtn'), true, '校验中…');
    try {
      const payload = JSON.parse($('records').value);
      const data = await jsonFetch('api/v1/records/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderRecords(data);
      $('recordsStatus').textContent = `完成：${data.summary.fail} 项问题`;
      toast('记录校验完成');
    } catch (error) {
      $('recordsStatus').textContent = '校验失败';
      showError(error);
    } finally {
      busy($('validateBtn'), false);
    }
  });

  $('stdSearch').addEventListener('input', renderStdList);
  $('stdTaskCancel').addEventListener('click', () => cancelTask('stdTask'));
  $('publishTaskCancel').addEventListener('click', () => cancelTask('publishTask'));
  $('stdList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-std-id]');
    if (!item) return;
    loadStandard(item.dataset.stdId).catch(showError);
  });
  $('itemsBody').addEventListener('click', (event) => {
    const row = event.target.closest('tr[data-i]');
    if (row) selectItem(Number(row.dataset.i));
  });
  $('parseBtn').addEventListener('click', async () => {
    const file = $('stdFile').files[0];
    if (!file) { toast('请先选择标准文档', 'info'); return; }
    busy($('parseBtn'), true, '解析中…');
    $('stdTaskError').classList.add('hidden');
    try {
      const form = new FormData();
      form.append('file', file);
      if ($('stdProject').value.trim()) form.append('project', $('stdProject').value.trim());
      const data = await jsonFetch('api/v1/standards/parse', { method: 'POST', body: form });
      if (data.task_id) {
        $('parseStatus').textContent = '已进入后台队列，可继续查看其他标准';
        renderTask('stdTask', { ...data, stage: 'queued', progress: 0 });
        const finished = await pollTask(data.task_id, 'stdTask');
        if (finished.status === 'succeeded') {
          const items = finished.result ? finished.result.items : 0;
          $('parseStatus').textContent = `解析完成：${items} 项`;
          toast(`标准解析完成：${items} 项`);
          await loadStandards(finished.result.standard_id);
        } else if (finished.status === 'failed') {
          $('parseStatus').textContent = '解析失败';
          toast(`标准解析失败：${finished.error}`, 'error');
        } else {
          $('parseStatus').textContent = '解析已取消';
        }
        return;
      }
      $('parseStatus').textContent = `解析完成：${data.summary.items} 项，${data.summary.clauses_traced} 条可溯源`;
      toast(`标准解析完成：${data.summary.items} 项`);
      await loadStandards(data.id);
    } catch (error) {
      $('parseStatus').textContent = '解析失败';
      showError(error);
    } finally {
      busy($('parseBtn'), false);
    }
  });
  $('saveBtn').addEventListener('click', async () => {
    if (!state.current || state.selectedIndex < 0) return;
    const item = state.current.items[state.selectedIndex];
    const standardId = state.current.id;
    busy($('saveBtn'), true, '保存中…');
    try {
      await jsonFetch(`api/v1/standards/${encodeURIComponent(standardId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reviewer: reviewerName(),
          items: [{ clause: item.clause, name: $('editName').value, requirement: $('editReq').value, method: $('editMethod').value }],
        }),
      });
      toast('复核修改已保存');
      await loadStandard(standardId, item.clause);
    } catch (error) {
      showError(error);
    } finally {
      busy($('saveBtn'), false);
    }
  });
  $('confirmBtn').addEventListener('click', async () => {
    if (!state.current || state.selectedIndex < 0) return;
    const item = state.current.items[state.selectedIndex];
    const standardId = state.current.id;
    busy($('confirmBtn'), true, '保存中…');
    try {
      await jsonFetch(`api/v1/standards/${encodeURIComponent(standardId)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer: reviewerName(), items: [{ clause: item.clause, reviewed: !item.reviewed }] }),
      });
      toast(item.reviewed ? '已取消确认' : '已确认该条款');
      await loadStandard(standardId, item.clause);
    } catch (error) { showError(error); } finally { busy($('confirmBtn'), false); }
  });
  $('addItemBtn').addEventListener('click', () => {
    ['addClause', 'addName', 'addRequirement', 'addMethod'].forEach((id) => { $(id).value = ''; });
    $('addForm').classList.remove('hidden');
    $('addClause').focus();
  });
  $('addCancelBtn').addEventListener('click', () => $('addForm').classList.add('hidden'));
  $('addSaveBtn').addEventListener('click', async () => {
    if (!state.current) return;
    const clause = $('addClause').value.trim();
    const name = $('addName').value.trim();
    if (!clause || !name) { toast('补充条款需要条款号和项目名称', 'info'); return; }
    const standardId = state.current.id;
    busy($('addSaveBtn'), true, '保存中…');
    try {
      await jsonFetch(`api/v1/standards/${encodeURIComponent(standardId)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer: reviewerName(), additions: [{
          clause, name, requirement: $('addRequirement').value, method: $('addMethod').value,
        }] }),
      });
      toast('补充条款已保存并留存为反馈样本');
      $('addForm').classList.add('hidden');
      await loadStandard(standardId, clause);
    } catch (error) { showError(error); } finally { busy($('addSaveBtn'), false); }
  });
  $('exportBtn').addEventListener('click', () => {
    if (!state.current) return;
    window.location = `api/v1/standards/${encodeURIComponent(state.current.id)}/export.xlsx`;
    toast('已开始导出 Excel', 'info');
  });
  $('publishBtn').addEventListener('click', async () => {
    if (!state.current || state.current.published) return;
    const standard = state.current;
    const confirmed = await confirmDialog({
      title: '发布到知识库？',
      message: `将把「${standard.meta.name || standard.meta.standard_no}」的 ${standard.items.length} 个条款切成知识片段；重复发布会更新同一条知识。`,
      confirmText: '发布',
    });
    if (!confirmed) return;
    busy($('publishBtn'), true, '发布中…');
    $('publishTaskError').classList.add('hidden');
    try {
      const data = await jsonFetch(`api/v1/standards/${encodeURIComponent(standard.id)}/publish`, { method: 'POST' });
      if (data.task_id) {
        renderTask('publishTask', { ...data, stage: 'queued', progress: 0 });
        const finished = await pollTask(data.task_id, 'publishTask');
        if (finished.status === 'succeeded') {
          const chunks = finished.result ? finished.result.chunks : 0;
          toast(`已发布 ${chunks} 个知识片段${finished.result && finished.result.weknora ? '，并同步到 WeKnora' : '（当前为本地演示索引）'}`);
          const clause = state.selectedIndex >= 0 ? standard.items[state.selectedIndex].clause : '';
          await loadStandard(standard.id, clause);
        } else if (finished.status === 'failed') {
          toast(`发布失败：${finished.error}`, 'error');
        }
        return;
      }
      toast(`已发布 ${data.chunks} 个知识片段${data.weknora ? '，并同步到 WeKnora' : '（当前为本地演示索引）'}`);
      const clause = state.selectedIndex >= 0 ? standard.items[state.selectedIndex].clause : '';
      await loadStandard(standard.id, clause);
    } catch (error) {
      showError(error);
    } finally {
      busy($('publishBtn'), false);
    }
  });
  $('unpublishBtn').addEventListener('click', async () => {
    if (!state.current || !state.current.published) return;
    const standard = state.current;
    const confirmed = await confirmDialog({
      title: '从知识库下架？',
      message: `将删除「${standard.meta.name || standard.meta.standard_no}」在知识库中的片段，问答不再引用；本地解析与复核记录保留，可重新发布。`,
      confirmText: '下架',
      tone: 'danger',
    });
    if (!confirmed) return;
    busy($('unpublishBtn'), true, '下架中…');
    try {
      const data = await jsonFetch(`api/v1/standards/${encodeURIComponent(standard.id)}/unpublish`, { method: 'POST' });
      const removed = (data.removed_knowledge_ids || []).length;
      toast(`已下架${removed ? `，删除 ${removed} 条知识库片段` : ''}`);
      const clause = state.selectedIndex >= 0 ? standard.items[state.selectedIndex].clause : '';
      await loadStandard(standard.id, clause);
    } catch (error) {
      showError(error);
    } finally {
      busy($('unpublishBtn'), false);
    }
  });
  $('stdVersionList').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-rollback]');
    if (!button || !state.current) return;
    const standard = state.current;
    const version = Number(button.dataset.rollback);
    const confirmed = await confirmDialog({
      title: `回滚到修订 r${version}？`,
      message: '会以该修订的条款生成一个新的当前修订，复核历史保留；如已发布会先下架，回滚后需重新发布。',
      confirmText: '回滚',
      tone: 'danger',
    });
    if (!confirmed) return;
    busy(button, true, '回滚中…');
    try {
      const updated = await jsonFetch(`api/v1/standards/${encodeURIComponent(standard.id)}/rollback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version }),
      });
      toast(`已回滚到 r${version}，当前修订 r${updated.revision}${updated.published === false ? '；标准已下架，请复核后重新发布' : ''}`);
      await loadStandard(standard.id);
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
    }
  });

  $('quoteBtn').addEventListener('click', async () => {
    const file = $('quoteFile').files[0];
    if (!file) { toast('请先选择报价输入文件', 'info'); return; }
    const template = $('quoteTemplate').files[0];
    busy($('quoteBtn'), true, '生成中…');
    $('quoteStatus').textContent = '';
    try {
      const form = new FormData();
      form.append('file', file);
      if (template) form.append('template', template);
      if ($('quoteProject').value.trim()) form.append('project', $('quoteProject').value.trim());
      const data = await jsonFetch('api/v1/quotes/generate', { method: 'POST', body: form });
      renderQuote(data);
      $('quoteStatus').textContent = '完成';
      toast(data.summary && data.summary.unpriced_lines ? '已生成 CRM 草稿，含待补单价行' : '报价已生成', data.summary && data.summary.unpriced_lines ? 'info' : undefined);
      if (can('quote:generate')) await loadQuotes();
    } catch (error) {
      $('quoteStatus').textContent = '生成失败';
      showError(error);
    } finally {
      busy($('quoteBtn'), false);
    }
  });
  $('quoteResult').addEventListener('click', (event) => {
    const button = event.target.closest('[data-download]');
    if (!button) return;
    const kind = button.dataset.kind === 'crm' ? 'crm' : 'sales';
    window.location = `api/v1/quotes/${encodeURIComponent(button.dataset.download)}/${kind}.xlsx`;
  });
  $('quoteHistory').addEventListener('click', (event) => {
    const button = event.target.closest('[data-quote-download]');
    if (!button) return;
    const kind = button.dataset.kind === 'crm' ? 'crm' : 'sales';
    window.location = `api/v1/quotes/${encodeURIComponent(button.dataset.quoteDownload)}/${kind}.xlsx`;
  });

  $('askBtn').addEventListener('click', askQuestion);
  $('question').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') askQuestion();
  });
  $('sampleQuestions').addEventListener('click', (event) => {
    const button = event.target.closest('[data-q]');
    if (!button) return;
    $('question').value = button.dataset.q;
    askQuestion();
  });
  $('copyAnswerBtn').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('answerBox').textContent || '');
      toast('已复制答案');
    } catch (error) {
      toast('复制失败，请手动选择文本', 'error');
    }
  });
  $('answerFeedback').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-answer-feedback]');
    if (!button || !state.lastAnswer) return;
    const rating = button.dataset.answerFeedback;
    const answer = state.lastAnswer;
    $$('#answerFeedback [data-answer-feedback]').forEach((item) => { item.disabled = true; });
    try {
      await jsonFetch('api/v1/standards/ask/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: answer.question,
          answer: answer.answer,
          rating,
          standard_id: (answer.citations[0] || {}).standard_id || '',
          clause: (answer.citations[0] || {}).clause || '',
          retrieval_mode: answer.retrieval_mode,
          citations: answer.citations.map((citation) => ({
            standard_id: citation.standard_id || '',
            clause: citation.clause || '',
          })),
        }),
      });
      $('answerFeedbackStatus').textContent = rating === 'helpful' ? '已记录：有帮助' : '已记录：没帮助';
      toast('感谢反馈');
      if (can('feedback:read')) loadFeedbackStats().catch(() => {});
    } catch (error) {
      $$('#answerFeedback [data-answer-feedback]').forEach((item) => { item.disabled = false; });
      showError(error);
    }
  });
  $('citations').addEventListener('click', (event) => {
    const citation = event.target.closest('[data-citation-standard]');
    if (!citation) return;
    const standardId = citation.dataset.citationStandard;
    switchTab('standards');
    loadStandards(standardId)
      .then(() => loadStandard(standardId, citation.dataset.citationClause || ''))
      .catch(showError);
  });

  $('adminTabs').addEventListener('click', (event) => {
    const button = event.target.closest('[data-admin-view]');
    if (button) switchAdminView(button.dataset.adminView);
  });
  $('reportConfigSave').addEventListener('click', () => saveConfigSection('report', $('reportConfigSave'), collectReportConfig));
  $('standardConfigSave').addEventListener('click', () => saveConfigSection('standard', $('standardConfigSave'), collectStandardConfig));
  $('quoteConfigSave').addEventListener('click', () => saveConfigSection('quote', $('quoteConfigSave'), collectQuoteConfig));
  $('businessConfigSave').addEventListener('click', () => saveConfigSection('business', $('businessConfigSave'), collectBusinessConfig));
  $('reportConfigReset').addEventListener('click', () => resetConfigSection('report', '审核规则'));
  $('standardConfigReset').addEventListener('click', () => resetConfigSection('standard', '解析规则'));
  $('quoteConfigReset').addEventListener('click', () => resetConfigSection('quote', '报价规则'));
  $('businessConfigReset').addEventListener('click', () => resetConfigSection('business', '业务规则'));
  $('userSearch').addEventListener('input', renderUsers);
  $('roleCards').addEventListener('click', (event) => {
    const card = event.target.closest('[data-role]');
    if (!card) return;
    state.userRoleFilter = state.userRoleFilter === card.dataset.role ? null : card.dataset.role;
    renderRoleCards();
    renderUsers();
  });
  $('userBody').addEventListener('change', async (event) => {
    const select = event.target.closest('.role-select');
    if (!select) return;
    try {
      await jsonFetch(`api/v1/auth/users/${encodeURIComponent(select.dataset.roleUser)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: select.value }),
      });
      toast(`已更新 ${select.dataset.roleUser} 的角色`);
      await loadAdmin();
    } catch (error) {
      showError(error);
      await loadAdmin();
    }
  });
  $('userBody').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const username = button.dataset.user;
    if (button.dataset.act === 'projects') {
      openProjectModal(username, button.dataset.projects || '');
    } else if (button.dataset.act === 'reset') {
      openResetModal(username);
    } else if (button.dataset.act === 'toggle') {
      const active = button.dataset.active === '1';
      const confirmed = await confirmDialog({
        title: active ? '停用账号？' : '启用账号？',
        message: active ? `停用后 ${username} 将立即无法访问系统；历史审计记录会保留。` : `启用后 ${username} 可以重新登录并按当前角色使用系统。`,
        confirmText: active ? '停用' : '启用',
        tone: active ? 'danger' : 'primary',
      });
      if (confirmed) await patchUser(username, { active: !active }, `已${active ? '停用' : '启用'} ${username}`);
    }
  });
  $('newUserBtn').addEventListener('click', () => {
    $('nuUsername').value = '';
    $('nuName').value = '';
    $('nuPassword').value = '';
    $('nuDept').value = '';
    $('nuProjects').value = '';
    $('nuRole').value = 'viewer';
    $('nuStatus').textContent = '';
    openModal('modalCreateUser');
  });
  $('nuGenPassword').addEventListener('click', () => {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    let body = '';
    bytes.forEach((byte) => { body += alphabet[byte % alphabet.length]; });
    $('nuPassword').value = `Demo@${body}`;
  });
  $('nuCreate').addEventListener('click', async () => {
    const username = $('nuUsername').value.trim();
    const password = $('nuPassword').value;
    if (!username || !password) { $('nuStatus').textContent = '请填写用户名和初始密码'; return; }
    busy($('nuCreate'), true, '创建中…');
    try {
      const data = await jsonFetch('api/v1/auth/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username,
          password,
          role: $('nuRole').value,
          display_name: $('nuName').value.trim(),
          department: $('nuDept').value.trim(),
          projects: $('nuProjects').value.split(/[,，\s]+/).filter(Boolean),
        }),
      });
      closeModal('modalCreateUser');
      toast(`已创建 ${data.user.username}（${ROLE_NAMES[data.user.role] || data.user.role}）`);
      await loadAdmin();
    } catch (error) {
      $('nuStatus').textContent = error.message;
    } finally {
      busy($('nuCreate'), false);
    }
  });
  $('projectPresets').addEventListener('click', (event) => {
    const button = event.target.closest('[data-project]');
    if (!button) return;
    const current = $('projectInput').value.split(/[,，\s]+/).filter(Boolean);
    if (!current.includes(button.dataset.project)) current.push(button.dataset.project);
    $('projectInput').value = current.join(', ');
  });
  $('projectSave').addEventListener('click', async () => {
    if (!state.projectUser) return;
    const projects = $('projectInput').value.split(/[,，\s]+/).filter(Boolean);
    busy($('projectSave'), true, '保存中…');
    try {
      await jsonFetch(`api/v1/auth/users/${encodeURIComponent(state.projectUser)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projects }),
      });
      closeModal('modalProject');
      toast(`已更新 ${state.projectUser} 的项目授权`);
      await loadAdmin();
    } catch (error) {
      $('projectStatus').textContent = error.message;
    } finally {
      busy($('projectSave'), false);
    }
  });
  $('resetGenerate').addEventListener('click', async () => {
    if (!state.resetUser) return;
    busy($('resetGenerate'), true, '生成中…');
    try {
      const data = await jsonFetch(`api/v1/auth/users/${encodeURIComponent(state.resetUser)}/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      $('resetResult').classList.remove('hidden');
      $('resetNotice').classList.add('hidden');
      $('resetPasswordOut').textContent = data.password;
      $('resetStatus').textContent = '';
      toast('临时密码已生成');
      await loadAdmin();
    } catch (error) {
      $('resetStatus').textContent = error.message;
    } finally {
      busy($('resetGenerate'), false);
      $('resetGenerate').disabled = true;
      $('resetGenerate').innerHTML = `${ic('check')}已生成`;
    }
  });
  $('copyResetPassword').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('resetPasswordOut').textContent || '');
      toast('临时密码已复制');
    } catch (error) {
      toast('复制失败，请手动选择', 'error');
    }
  });

  $('auQuery').addEventListener('click', () => loadAudit(true));
  $('auExport').addEventListener('click', () => {
    const query = new URLSearchParams(auditFilters());
    window.location = `api/v1/audit/export.csv?${query.toString()}`;
    toast('已开始导出审计 CSV', 'info');
  });
  $('auditPrev').addEventListener('click', () => {
    state.audit.offset = Math.max(0, state.audit.offset - state.audit.limit);
    loadAudit(false);
  });
  $('auditNext').addEventListener('click', () => {
    state.audit.offset += state.audit.limit;
    loadAudit(false);
  });
  $('auditBody').addEventListener('click', (event) => {
    const row = event.target.closest('tr[data-audit-index]');
    if (row) openAuditDetail(Number(row.dataset.auditIndex));
  });

  $$('[data-close]').forEach((button) => button.addEventListener('click', () => closeModal(button.dataset.close)));
  $$('dialog.modal').forEach((dialog) => {
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      if (dialog.id === 'modalConfirm') settleConfirm(false);
      else dialog.close();
    });
  });
  $('modalConfirm').addEventListener('cancel', (event) => {
    event.preventDefault();
    settleConfirm(false);
  });
  $('confirmOk').addEventListener('click', () => settleConfirm(true));
  $('confirmCancel').addEventListener('click', () => settleConfirm(false));
  $('confirmClose').addEventListener('click', () => settleConfirm(false));
}

/* ---------------------------------------------------------------- boot */
function boot() {
  $('auEvent').innerHTML = '<option value="">全部事件</option>' + EVENT_OPTIONS.map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
  bindEvents();
  const wanted = params.get('tab');
  const initialTab = ['report', 'standards', 'quote', 'knowledge', 'admin'].includes(wanted) ? wanted : 'report';
  const wantedView = params.get('view');
  const initialAdminView = ['users', 'audit', 'config'].includes(wantedView) ? wantedView : 'users';
  const wantedSection = params.get('section') || '';
  const initialConfigSection = initialAdminView === 'config' && adminConfigSection(wantedSection) ? wantedSection : '';
  if (initialTab === 'admin') state.adminView = initialAdminView;
  loadMe().then((signedIn) => {
    if (!signedIn) return;
    switchTab(initialTab);
    if (initialConfigSection && canConfig(initialConfigSection) && !$('panel-admin').classList.contains('hidden')) {
      syncAdminLocation('config', initialConfigSection);
      adminConfigLoad.then(() => {
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const meta = adminConfigSection(initialConfigSection);
          if (focusAdminConfigSection(initialConfigSection) && meta) toast(`已定位到「${meta.label}」`, 'info');
        }));
      });
    }
    loadStandards().then(() => resumeLatestTask()).catch((error) => {
      $('stdList').innerHTML = `<div class="muted pad-8">标准加载失败：${escapeHtml(error.message)}</div>`;
    });
  }).catch(showError);
}

boot();
