'use strict';

const $ = (id) => document.getElementById(id);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const params = new URLSearchParams(location.search);
let customerServicePreviewData = null;
let parsedQuoteReview = null;
let quoteParseReviewGeneration = 0;
let crmScheduleRows = [];
let crmScheduleCandidates = [];
const crmScheduleDraftGuard = window.ScheduleCandidate.createDraftGuard();
let crmQualificationRows = [];
let crmPriceRows = [];
let crmNameMappingRows = [];

const ROLE_NAMES = { customer: '新客户/小微客户', business_manager: '业务经理', admin: '系统管理员', technical: '技术部门' };
const ROLE_DESCRIPTIONS = {
  customer: '通过小程序办理本人企业的报价与委托',
  business_manager: '报价、委托审核、客户沟通与 CRM 报价维护',
  admin: '知识库、基础资料、全局规则、账号与统计管理',
  technical: '排期项目库、加急规则与技术支持',
};
const FIELD_NAMES = { name: '实检项目', requirement: '技术要求', method: '试验方法' };
const FEEDBACK_ACTIONS = { correct: '修正', confirm: '确认', supplement: '补充' };
const TEMPLATE_NAMES = { 'BYD-odor': '比亚迪气味报告模板', generic: '通用报告模板' };
const SEVERITY_NAMES = { high: '高', medium: '中', low: '低', info: '提示' };
const SEVERITY_TONES = { high: 'danger', medium: 'warn', low: 'neutral', info: 'neutral' };
const LAYER_LABELS = { deterministic: '确定性', crosscheck: '跨文档', partial: '部分规则', dsl: '规则 DSL', semantic: '语义' };
const QUOTE_HEADER_NAMES = { item: '试验项目', price: '单价', quantity: '数量', unit: '单位', sample: '样品名称' };
const ROLE_CODES = ['customer', 'business_manager', 'admin', 'technical'];
const EMPLOYEE_ROLE_CODES = ['business_manager', 'admin', 'technical'];
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
  customer_service_quote_parse_reviewed: '客服询价解析复核',
  standard_published: '发布标准',
  standard_unpublished: '下架标准',
  standard_ask_feedback: '问答反馈',
  standard_rolled_back: '回滚标准修订',
  standard_exported: '导出标准',
  standard_project_assigned: '标准项目授权',
  standard_asked: '知识问答',
  quote_generated: '生成报价',
  quote_downloaded: '下载报价',
  customer_service_price_candidate_reviewed: '客服价格候选人工复核',
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
  ['customer_service_price_candidate_reviewed', '客服价格候选人工复核'],
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
  customerRegistrations: [],
  customerQuoteInquiries: [],
  myQuoteInquiries: [],
  quoteInquiryId: '',
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
    const error = new Error(detail || `请求失败（${response.status}）`);
    error.statusCode = response.status;
    throw error;
  }
  return data;
}
function can(action) {
  return Boolean(state.me && Array.isArray(state.me.permissions) && state.me.permissions.includes(action));
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
    $('reportRoleNotice').innerHTML = `${ic('lock')}<span>当前账号未获报告审核权限；该权限由管理员逐账号确认并记录原因。</span>`;
  }
  $('stdUploadBlock').classList.toggle('hidden', !canParse);
  $('stdRoleNotice').classList.toggle('hidden', canParse);
  if (!canParse) {
    $('stdRoleNotice').innerHTML = `${ic('eye')}<span>标准解析与复核按账号授权；未获授权的操作不会开放。</span>`;
  }
  $('publishBtn').classList.toggle('hidden', !canPublish);
  $('unpublishBtn').classList.toggle('hidden', !canPublish);
  ['saveBtn', 'confirmBtn', 'addItemBtn'].forEach((id) => $(id).classList.toggle('hidden', !canReview));
  ['editName', 'editReq', 'editMethod'].forEach((id) => { $(id).disabled = !canReview; });
  $('quoteUploadCard').classList.toggle('hidden', !canQuote);
  $('quoteInquiryClientCard').classList.toggle('hidden', !canQuote);
  $('quoteResultCard').classList.toggle('hidden', !canQuote);
  $('quoteDemoPreviewCard').classList.toggle('hidden', !canQuote);
  $('quoteHistoryCard').classList.toggle('hidden', !canQuote);
  $('quoteRoleNotice').classList.toggle('hidden', canQuote);
  if (!canQuote) {
    $('quoteRoleNotice').innerHTML = `${ic('lock')}<span>当前账号没有业务经理报价权限。</span>`;
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
  admin: { title: '业务工作区' },
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
    applyNavigationPermissions();
    if (!can('user:manage')) {
      $('adminPageTitle').textContent = '业务工作区';
      $('adminPageDesc').textContent = '处理你负责的客户服务、报价、CRM 价格或排期任务。';
    }
    $('adminNavSection').classList.toggle('hidden', !canAnyAdminView());
    return true;
  } catch (error) {
    $('userBox').innerHTML = `<span class="avatar">!</span><div class="user-meta"><b>未登录</b><span>请重新登录</span></div>`;
    return false;
  }
}
function switchTab(name) {
  if (!TAB_META[name]) name = 'report';
  if (!canUseTab(name)) name = defaultTab();
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
  if (name === 'quote' && can('quote:generate')) {
    loadQuotes().catch(showError);
    loadMyQuoteInquiries();
  }
  if (name === 'admin') {
    if (can('user:manage')) loadAdmin().catch(showError);
    switchAdminView(can('user:manage') ? state.adminView : defaultAdminView());
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
function canAnyAdminView() {
  return [
    'user:manage', 'config:read', 'customer-service:registration:review',
    'customer-service:order:review', 'customer-service:quote:manage',
    'customer-service:conversation:manage', 'schedule:manage', 'crm:base:manage',
    'crm:prices:manage',
  ].some(can);
}
function canUseTab(name) {
  if (name === 'report') return can('report:audit') || can('report:read');
  if (name === 'standards') return can('standard:read') || can('standard:parse');
  if (name === 'quote') return can('quote:generate') || can('quote:parse');
  if (name === 'knowledge') return can('standard:ask');
  if (name === 'admin') return canAnyAdminView();
  return false;
}
function defaultTab() {
  return ['quote', 'knowledge', 'standards', 'admin', 'report'].find(canUseTab) || 'report';
}
function applyNavigationPermissions() {
  $$('.nav-item').forEach((item) => item.classList.toggle('hidden', !canUseTab(item.dataset.tab)));
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

async function parseSelectedQuoteLines(projectName) {
  const file = $('quoteFile').files[0];
  if (!file) throw new Error('请先在报价输入区选择文件');
  if (!projectName) throw new Error('请填写检测项目名称');
  if (!parsedQuoteReview || parsedQuoteReview.file !== file || parsedQuoteReview.projectName !== projectName) {
    throw new Error('请先解析当前文件和项目，再复核并确认明细');
  }
  if (!parsedQuoteReview.reviewedPayload) throw new Error('请先点击“确认复核并留痕”后再预览');
  return parsedQuoteReview.reviewedPayload;
}

function resetQuoteParseReview(message = '') {
  quoteParseReviewGeneration += 1;
  parsedQuoteReview = null;
  const target = $('quoteParseReview');
  if (!target) return;
  target.classList.toggle('hidden', !message);
  target.innerHTML = message ? `<div class="banner info">${escapeHtml(message)}</div>` : '';
}

function renderQuoteParseReview() {
  if (!parsedQuoteReview) return;
  const target = $('quoteParseReview');
  const rows = parsedQuoteReview.lines.map((line, index) => `
    <tr>
      <td>${index + 1}<br><small>来源行 ${escapeHtml(line.source_row || '未提供')}</small><br><small>${line.confidence === null || line.confidence === undefined ? '无置信度' : `置信 ${escapeHtml((Number(line.confidence) * 100).toFixed(1))}%`}</small></td>
      <td><input type="text" maxlength="500" data-parse-index="${index}" data-parse-field="item_name" value="${escapeHtml(line.item_name)}" aria-label="第 ${index + 1} 行项目"></td>
      <td><input type="text" maxlength="300" data-parse-index="${index}" data-parse-field="standard" value="${escapeHtml(line.standard)}" aria-label="第 ${index + 1} 行标准"></td>
      <td><input type="text" maxlength="500" data-parse-index="${index}" data-parse-field="sample_name" value="${escapeHtml(line.sample_name)}" aria-label="第 ${index + 1} 行样品"></td>
      <td><input type="number" min="0" step="any" data-parse-index="${index}" data-parse-field="quantity" value="${line.quantity === null ? '' : escapeHtml(line.quantity)}" aria-label="第 ${index + 1} 行数量"></td>
      <td><input type="text" maxlength="80" data-parse-index="${index}" data-parse-field="unit" value="${escapeHtml(line.unit)}" aria-label="第 ${index + 1} 行单位"></td>
    </tr>`).join('');
  const warnings = (parsedQuoteReview.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  target.innerHTML = `<div class="banner info">文件 ${escapeHtml(parsedQuoteReview.filename)} · ${escapeHtml(parsedQuoteReview.parseMethod)} · ${parsedQuoteReview.lines.length} 行。原始单价不进入本次 CRM 价格预览；低置信/缺项请人工核对。</div>${warnings ? `<ul class="check-list quote-warnings">${warnings}</ul>` : ''}<div class="table-wrap"><table><thead><tr><th>证据</th><th>检测项目</th><th>标准/条款</th><th>样品</th><th>数量</th><th>单位</th></tr></thead><tbody>${rows}</tbody></table></div><div class="row mt-16"><button type="button" class="btn btn-primary" id="quoteParseReviewConfirmBtn">确认复核并留痕</button><span id="quoteParseReviewStatus" class="muted">修改前后值将记录到审计日志；来源行和解析置信度由服务端固定。</span></div>`;
  target.classList.remove('hidden');
}

async function beginQuoteParseReview() {
  const file = $('quoteFile').files[0];
  const projectName = $('quoteDemoProject').value.trim();
  if (!file) throw new Error('请先在报价输入区选择文件');
  if (!projectName) throw new Error('请填写检测项目名称');
  const requestGeneration = ++quoteParseReviewGeneration;
  const form = new FormData();
  form.append('file', file);
  form.append('project', projectName);
  const parsed = await jsonFetch('api/v1/quotes/parse', { method: 'POST', body: form });
  if (requestGeneration !== quoteParseReviewGeneration
    || $('quoteFile').files[0] !== file
    || $('quoteDemoProject').value.trim() !== projectName) {
    throw new Error('文件或项目已变化，已丢弃过期解析结果，请重新解析');
  }
  if (!Array.isArray(parsed.items) || !parsed.items.length) throw new Error('文件未解析出可匹配的报价明细');
  parsedQuoteReview = {
    file, filename: file.name, projectName,
    sourceSha256: parsed.source_sha256,
    parseToken: parsed.parse_token,
    generation: requestGeneration,
    editRevision: 0,
    parseMethod: parsed.parse_method || '文件解析', warnings: parsed.warnings || [],
    lines: parsed.items.map((item, index) => ({
      line_index: index,
      item_name: item.item_name || '', standard: item.standard || '',
      sample_name: item.sample_name || parsed.sample_name || '',
      quantity: item.quantity ?? null, unit: item.unit || '',
      source_row: item.source_row || index + 1, confidence: item.confidence ?? null,
    })),
  };
  renderQuoteParseReview();
  return parsedQuoteReview;
}

function collectQuoteParseEdits() {
  if (!parsedQuoteReview) throw new Error('请先解析文件');
  return parsedQuoteReview.lines.map((line, index) => {
    const value = (field) => $(`#quoteParseReview [data-parse-index="${index}"][data-parse-field="${field}"]`).value.trim();
    const quantityText = value('quantity');
    const quantity = quantityText ? Number(quantityText) : null;
    if (quantity !== null && (!Number.isFinite(quantity) || quantity <= 0)) throw new Error(`第 ${index + 1} 行数量必须大于 0，或留空待补`);
    return {
      line_index: index, source_row: line.source_row,
      item_name: value('item_name'), standard: value('standard'), sample_name: value('sample_name'),
      quantity, unit: value('unit'),
    };
  });
}

async function saveQuoteParseReview() {
  const reviewState = parsedQuoteReview;
  const file = $('quoteFile').files[0];
  const projectName = $('quoteDemoProject').value.trim();
  if (!reviewState || reviewState.file !== file || reviewState.projectName !== projectName) throw new Error('所选文件或项目已变化，请重新解析');
  const requestGeneration = quoteParseReviewGeneration;
  const editRevision = reviewState.editRevision;
  const edits = collectQuoteParseEdits();
  const form = new FormData();
  form.append('file', file);
  form.append('project', projectName);
  form.append('source_sha256', reviewState.sourceSha256 || '');
  form.append('parse_token', reviewState.parseToken || '');
  form.append('edits', JSON.stringify(edits));
  const reviewed = await jsonFetch('api/v1/quotes/review', { method: 'POST', body: form });
  if (parsedQuoteReview !== reviewState
    || requestGeneration !== quoteParseReviewGeneration
    || reviewState.editRevision !== editRevision
    || $('quoteFile').files[0] !== file
    || $('quoteDemoProject').value.trim() !== projectName) {
    throw new Error('文件、项目或复核内容在提交期间发生变化；本次审计已记录，但结果未绑定到当前明细，请重新确认');
  }
  parsedQuoteReview.reviewedPayload = {
    project_name: projectName, review_id: reviewed.review_id,
    lines: reviewed.lines.map((line) => ({
      item_name: line.item_name, standard: line.standard, sample_name: line.sample_name,
      quantity: line.quantity, unit: line.unit, source_row: line.source_row, confidence: line.confidence,
    })),
  };
  $('quoteParseReviewStatus').textContent = `已记录复核 ${reviewed.review_id}；源文件 SHA-256 ${reviewed.source_sha256.slice(0, 16)}…；未生成报价或写回 CRM。`;
  $('quoteParseReviewConfirmBtn').disabled = true;
  return parsedQuoteReview.reviewedPayload;
}

function renderCustomerServicePreview(data) {
  customerServicePreviewData = data;
  const hasLineArray = Boolean(data && Array.isArray(data.lines));
  const lines = hasLineArray ? data.lines : [];
  const sourceFlagsValid = hasLineArray && lines.every((line) =>
    Boolean(line && Array.isArray(line.source_refs)
      && line.source_refs.every((source) => source && source.synthetic === (data.environment === 'synthetic')))
  );
  const syntheticPreview = data && data.environment === 'synthetic' && data.demo_only === true;
  const crmPreview = data && data.environment === 'internal' && data.demo_only === false
    && ['excel', 'csv', 'base'].includes(data.source_mode) && Boolean(data.source_revision)
    && data.input_origin === 'parsed_quote';
  if ((!syntheticPreview && !crmPreview) || !sourceFlagsValid) {
    throw new Error('服务端预览环境或来源标记无效，已拒绝显示');
  }
  const statusNames = {
    matched: data.environment === 'synthetic' ? '已命中（演示）' : '已匹配（只读）',
    missing: '未命中', expired: '已过期', ambiguous: '待确认',
    unknown: '未知', standard_scope: '标准级范围', project_scope_only: '仅项目级范围',
    not_found: '未找到', source_unavailable: '数据源不可用',
    preview_only: '仅预览', ask_customer: '补充信息', manual_review: '转人工', block: '阻断',
  };
  const rows = lines.map((line, lineIndex) => {
    const request = line.request || {};
    const evidence = line.input_evidence || {};
    const price = line.price_preview;
    const schedule = line.schedule_preview;
    const qualification = line.qualification_preview;
    const currencyLabel = price && price.currency === 'CNY'
      ? '¥'
      : escapeHtml(price && price.currency ? price.currency : '币种待确认');
    const priceText = line.price_status === 'matched' && price
      ? `${currencyLabel} ${escapeHtml(money(price.unit_price))} · ${escapeHtml(price.tax_inclusive === true ? '含税' : price.tax_inclusive === false ? '税外' : '税口径待确认')}`
      : statusNames[line.price_status] || line.price_status;
    const priceEvidence = price && data.environment === 'internal'
      ? `<br><small>${price.reference_quantity === null || price.reference_quantity === undefined ? '' : `CRM记录数量 ${escapeHtml(price.reference_quantity)} · `}${escapeHtml(price.sample_match_level === 'exact' ? '样品名精确匹配' : price.sample_match_level === 'fallback' ? '样品名未命中，按项目/项目行回退' : '未提供样品名')}</small>`
      : '';
    const priceCandidates = (line.price_candidates || []).map((candidate) => {
      const candidateCurrency = candidate.currency === 'CNY' ? '¥' : candidate.currency || '币种待确认';
      const candidatePrice = typeof candidate.unit_price === 'number' && Number.isFinite(candidate.unit_price)
        ? `${escapeHtml(candidateCurrency)} ${escapeHtml(money(candidate.unit_price))}`
        : '单价待核验';
      const sampleMatch = request.sample_name && candidate.sample_name === request.sample_name
        ? '样品名相同'
        : request.sample_name
          ? `样品：${escapeHtml(candidate.sample_name || '未记录')}`
          : `样品：${escapeHtml(candidate.sample_name || '未记录')}`;
      const reviewButton = data.environment === 'internal'
        ? `<button type="button" class="btn btn-secondary btn-sm" data-cs-price-review="1" data-line-index="${lineIndex}" data-candidate-id="${escapeHtml(candidate.record_id || '')}">记录本次人工选择</button>`
        : '';
      return `<li>${sampleMatch} · ${candidatePrice} · 记录数量 ${escapeHtml(candidate.reference_quantity ?? '未提供')} · 有效至 ${escapeHtml(candidate.valid_until || '未提供')} · ${escapeHtml(candidate.record_id || '无来源编号')} ${reviewButton}</li>`;
    }).join('');
    const candidateDetails = priceCandidates
      ? `<details class="quote-candidates"><summary>${(line.price_candidates || []).length} 条价格候选，待人工核验</summary><ul>${priceCandidates}</ul></details>`
      : '';
    const scheduleText = schedule
      ? `${escapeHtml(statusNames[line.schedule_status] || line.schedule_status || '')}<br><small>${escapeHtml(schedule.start_date || '未提供')} – ${escapeHtml(schedule.end_date || '未提供')}</small>`
      : escapeHtml(statusNames[line.schedule_status] || line.schedule_status || '');
    const qualificationText = qualification
      ? `${escapeHtml(statusNames[line.qualification_status] || line.qualification_status || '')}<br><small>${escapeHtml(qualification.certificate_no || '')}${qualification.standard ? ` · ${escapeHtml(qualification.standard)}` : qualification.note ? ` · ${escapeHtml(qualification.note)}` : ' · 未确认到具体标准'}</small>`
      : escapeHtml(statusNames[line.qualification_status] || line.qualification_status || '');
    const refs = (line.source_refs || []).map((source) =>
      `${escapeHtml(source.record_id)} · ${escapeHtml(source.revision)}${source.valid_until ? ` · 有效至 ${escapeHtml(source.valid_until)}` : ''}${source.synthetic ? ' · 合成' : ' · 非合成来源'}`
    ).join('<br>') || '无来源记录';
    const missing = (line.missing_fields || []).map(escapeHtml).join('、') || '无';
    const mappedName = line.matched_item_name && line.matched_item_name !== request.item_name
      ? `<br><small>精确映射为：${escapeHtml(line.matched_item_name)}</small>` : '';
    const parseEvidence = evidence.parse_confidence === null || evidence.parse_confidence === undefined
      ? ''
      : `<br><small>解析置信度 ${escapeHtml((Number(evidence.parse_confidence) * 100).toFixed(1))}%${evidence.source_row ? ` · 来源行 ${escapeHtml(evidence.source_row)}` : ''}</small>`;
    return `<tr>
      <td><b>${escapeHtml(request.project_name || '')}</b><br>${escapeHtml(request.item_name || '')}${mappedName}${request.sample_name ? `<br><small>样品：${escapeHtml(request.sample_name)}</small>` : ''}<br><small>${escapeHtml(request.standard || '')} · ${escapeHtml(request.quantity ?? '数量缺失')} ${escapeHtml(request.unit || '')}</small>${parseEvidence}</td>
      <td>${priceText}${priceEvidence}<br><small>${escapeHtml(statusNames[line.price_status] || line.price_status || '')}</small>${candidateDetails}</td>
      <td>${scheduleText}</td>
      <td>${qualificationText}</td>
      <td>${refs}</td>
      <td>${missing}</td>
      <td>${escapeHtml(statusNames[line.action] || line.action || '')}</td>
    </tr>`;
  }).join('');
  const warnings = (data.warnings || []).map((warning) => `<li>${escapeHtml(warning)}</li>`).join('');
  const inputNote = syntheticPreview
    ? (data.input_origin === 'parsed_quote'
      ? '解析明细来自所选文件，仅用于与合成目录比对；不会作为真实 CRM 命中。'
      : '使用仓库内置的合成询价样例。')
    : `使用当前配置的 CRM 只读适配器（${data.source_mode}，数据版本 ${data.source_revision}）；这不代表 CRM API 已接通或数据已通过客户验收。`;
  $('quoteDemoPreviewNotice').innerHTML = `${ic('alert')}<span><b>${syntheticPreview ? '合成数据，仅限内部演示。' : '当前 CRM 只读数据，仅限内部复核。'}</b> ${escapeHtml(inputNote)} 不生成客户报价，不计算总价或规则折扣，也不写回 CRM。</span>`;
  $('quoteDemoPreviewNotice').classList.remove('hidden');
  $('quoteDemoPreview').innerHTML = `
    <ul class="check-list quote-warnings">${warnings}</ul>
    <div class="table-wrap"><table>
      <thead><tr><th>需求项目</th><th>价格状态</th><th>排期状态</th><th>资质状态</th><th>来源记录</th><th>待补内容</th><th>建议动作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="muted">样例没有明细</td></tr>'}</tbody>
    </table></div>
    <div class="notice-box mt-16">金额汇总：未计算 · 规则版本：${escapeHtml((data.rule_snapshot || {}).version || '未提供')}</div>
    <details class="raw-details"><summary>查看本次预览原始数据</summary><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></details>`;
  $('quoteDemoPreview').classList.remove('hidden');
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
  $('roleCards').innerHTML = ROLE_CODES.map((role) => `
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
    const options = EMPLOYEE_ROLE_CODES.map((role) => `<option value="${role}" ${user.role === role ? 'selected' : ''}>${ROLE_NAMES[role]}</option>`).join('');
    const projects = (user.projects || []).map((project) => `<span class="chip">${project === '*' ? '全部项目' : escapeHtml(project)}</span>`).join('') || (user.role === 'admin' ? '<span class="muted">管理员全局权限</span>' : user.project_scope_enforced ? '<span class="muted">无项目授权（拒绝项目数据）</span>' : '<span class="muted">历史账号范围待确认</span>');
    const status = user.active ? '<span class="pill success">启用</span>' : '<span class="pill neutral">停用</span>';
    return `<tr>
      <td><div class="user-cell"><span class="avatar">${escapeHtml(initials(name))}</span><div><span class="u-name">${escapeHtml(name)}</span><span class="u-login">@${username}${user.restricted ? ' · 项目受限' : ''}</span></div></div></td>
      <td>${user.role === 'customer' ? '<span class="muted">小程序账号</span>' : `<select class="role-select" data-role-user="${username}">${options}</select>`}</td>
      <td>${escapeHtml(user.department || '—')}</td>
      <td>${projects}</td>
      <td>${status}${user.locked ? '<span class="pill danger">锁定</span>' : ''}</td>
      <td class="actions">
        <button type="button" class="btn btn-secondary btn-sm" data-act="review-permissions" data-user="${username}" data-permissions="${escapeHtml((user.review_permissions || []).join(','))}">审核授权${(user.review_permissions || []).length ? ` (${user.review_permissions.length})` : ''}</button>
        <button type="button" class="btn btn-secondary btn-sm" data-act="oidc" data-user="${username}" data-issuers="${escapeHtml((user.oidc_issuers || []).join('|'))}">SSO${user.oidc_linked ? '已绑定' : '未绑定'}</button>
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
const REGISTRATION_STATUS_LABELS = {
  draft: '草稿', submitted: '待审核', needs_info: '待补件', approved: '已批准', rejected: '已拒绝',
};
function registrationReviewPendingKey(registrationId) {
  return `p0-registration-review:${String(state.me?.username || '')}:${registrationId}`;
}
function readPendingRegistrationReview(registrationId) {
  const stored = sessionStorage.getItem(registrationReviewPendingKey(registrationId));
  return stored ? JSON.parse(stored) : null;
}
async function loadCustomerRegistrations() {
  const body = $('registrationBody');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="10" class="muted">正在读取演示注册…</td></tr>';
  const status = $('registrationStatusFilter').value;
  try {
    const result = await jsonFetch('api/v1/customer-service/demo/admin/registrations');
    const allRows = result.registrations || [];
    state.customerRegistrations = filterReviewQueueItems(allRows, status, (item) => {
      try { return Boolean(readPendingRegistrationReview(item.registration_id)); }
      catch (error) { return true; }
    });
    if (!state.customerRegistrations.length) {
      body.innerHTML = '<tr><td colspan="10" class="muted">当前筛选下没有演示登记</td></tr>';
      return;
    }
    body.innerHTML = state.customerRegistrations.map((item) => {
      let pendingReview = null;
      try { pendingReview = readPendingRegistrationReview(item.registration_id); } catch (error) { pendingReview = { unreadable: true }; }
      const risk = item.rule_review || {};
      const routing = item.assignment_preview || {};
      const ruleRevision = risk.status === 'candidate'
        ? `data-registration-rule-revision="${escapeHtml(risk.config_revision)}" data-registration-revision="${escapeHtml(item.revision ?? 1)}"`
        : '';
      const actions = pendingReview
        ? `<button class="btn btn-warning btn-sm" data-registration-review-retry="${escapeHtml(item.registration_id)}" ${pendingReview.unreadable ? 'disabled' : ''}>${pendingReview.unreadable ? '重试记录不可读' : '恢复上次审核'}</button>`
        : item.status === 'submitted' && risk.status === 'candidate'
        ? `<div class="row"><button class="btn btn-secondary btn-sm" data-registration-decision="needs_info" data-registration-id="${escapeHtml(item.registration_id)}" ${ruleRevision}>补件</button><button class="btn btn-primary btn-sm" data-registration-decision="approved" data-registration-id="${escapeHtml(item.registration_id)}" ${ruleRevision}>批准</button><button class="btn btn-danger btn-sm" data-registration-decision="rejected" data-registration-id="${escapeHtml(item.registration_id)}" ${ruleRevision}>拒绝</button></div>`
        : (item.status === 'submitted' ? '规则不可用，暂不能审核' : (item.status === 'needs_info' ? '等待客户补件' : '—'));
      const riskSignals = risk.status !== 'candidate' ? ['规则配置不可用'] : [
        risk.company_list_conflict ? '黑白名单冲突候选' : (risk.blacklist_match ? '黑名单候选' : ''),
        !risk.company_list_conflict && risk.whitelist_match ? '白名单候选' : '',
        risk.whitelist_keyword_match ? '重点客户关键词候选' : '',
        risk.intermediary_keyword_match ? '中介关键词候选' : '',
      ].filter(Boolean);
      const riskHint = risk.status === 'candidate' && !riskSignals.length
        ? '当前规则未命中（不代表无风险）'
        : riskSignals.join('、');
      const riskCell = `<span class="pill">${escapeHtml(riskHint)}</span><div class="muted">版本 ${escapeHtml(risk.config_revision ?? '未知')}；仅供人工复核</div>`;
      const routeLabels = { matched_candidate: '地区匹配候选', backup_candidate: '备用经理候选', unconfigured: '未启用', unmatched: '无匹配/无备用', unavailable: '配置不可用', ambiguous: '映射冲突' };
      const routeText = routing.candidate_manager_id
        ? `${routeLabels[routing.status] || routing.status}：${routing.candidate_manager_id}`
        : (routeLabels[routing.status] || '未配置');
      const routeCell = `<span class="pill">${escapeHtml(routeText)}</span><div class="muted">配置 ${escapeHtml(routing.config_revision ?? '未知')}；${routing.assigned ? '已指派' : '仅候选'}；${routing.notification_sent ? '已通知' : '未通知'}</div>`;
      return `<tr><td>${escapeHtml(item.registration_id)}</td><td>${escapeHtml(item.company_name)}</td><td>${escapeHtml(item.contact_name)}</td><td>${escapeHtml(item.region)}</td><td>${escapeHtml(item.notes || '—')}</td><td>${riskCell}</td><td>${routeCell}</td><td><span class="pill">${escapeHtml(REGISTRATION_STATUS_LABELS[item.status] || item.status)}</span></td><td>${escapeHtml(item.updated_at)}</td><td>${actions}</td></tr>`;
    }).join('');
  } catch (error) {
    body.innerHTML = `<tr><td colspan="10" class="muted">${escapeHtml(error.message || '演示注册服务暂不可用')}</td></tr>`;
  }
}
async function decideCustomerRegistration(registrationId, decision, ruleConfigRevision = null, registrationRevision = null) {
  const pendingKey = registrationReviewPendingKey(registrationId);
  let pending = null;
  try { pending = readPendingRegistrationReview(registrationId); }
  catch (error) { toast('上次登记审核重试记录不可读取，已停止发送', 'error'); return; }
  if (!pending && !decision) { toast('本机没有待恢复的登记审核记录，请刷新队列', 'error'); return; }
  if (pending && pending.unreadable) { toast('上次登记审核重试记录不可读取，已停止发送', 'error'); return; }
  if (pending && (!pending.operationId || !pending.payload || !pending.fingerprint
      || pending.fingerprint !== JSON.stringify(pending.payload))) {
    toast('上次登记审核重试记录不完整，已停止发送', 'error'); return;
  }
  if (pending && decision && pending.payload.decision !== decision) {
    toast('上次审核结果尚未确认，请先恢复原操作', 'error'); return;
  }
  if (!pending) {
    const label = REGISTRATION_STATUS_LABELS[decision] || decision;
    const reason = window.prompt(`请填写“${label}”原因（必填）：`);
    if (reason === null) return;
    if (!reason.trim()) { toast('审核原因不能为空', 'error'); return; }
    const payload = {
      decision, reason: reason.trim(), rule_config_revision: ruleConfigRevision,
      registration_revision: registrationRevision,
    };
    try {
      pending = {
        payload, fingerprint: JSON.stringify(payload),
        operationId: createSecureOperationId('registration-review', window.crypto),
      };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
    } catch (error) { toast(error.message || '无法保存审核重试编号，未提交', 'error'); return; }
  }
  try {
    const result = await jsonFetch(`api/v1/customer-service/demo/admin/registrations/${encodeURIComponent(registrationId)}/review`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.operationId },
      body: JSON.stringify(pending.payload),
    });
    try { sessionStorage.removeItem(pendingKey); } catch (error) { toast('审核已记录，但本机待办未能清除；请刷新恢复结果', 'info'); }
    toast(result.idempotent_replay ? '已恢复上次审核结果' : `已记录：${REGISTRATION_STATUS_LABELS[pending.payload.decision] || pending.payload.decision}`);
    await loadCustomerRegistrations();
  } catch (error) {
    if (error.statusCode === 409 || (error.statusCode >= 400 && error.statusCode < 500)) {
      try { sessionStorage.removeItem(pendingKey); } catch (storageError) { /* Keep the operation if cleanup fails. */ }
      toast(`${error.message}；请刷新队列后再决定`, 'error');
    } else toast(error.message, 'error');
  }
}
const ORDER_STATUS_LABELS = { submitted: '待审核', needs_info: '待补件', approved: '待客户确认（演示）', rejected: '已拒绝（演示）', customer_confirmed: '客户已确认（演示）', customer_declined: '客户暂不接受（演示）' };
function orderReviewPendingKey(orderId) {
  return `p0-order-review:${String(state.me?.username || '')}:${orderId}`;
}
function readPendingOrderReview(orderId) {
  const stored = sessionStorage.getItem(orderReviewPendingKey(orderId));
  return stored ? JSON.parse(stored) : null;
}
async function loadCustomerOrders() {
  const body = $('orderBody');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="7" class="muted">正在读取演示委托…</td></tr>';
  const status = $('orderStatusFilter').value;
  try {
    const result = await jsonFetch('api/v1/customer-service/demo/admin/orders');
    const allRows = result.orders || [];
    const rows = filterReviewQueueItems(allRows, status, (item) => {
      try { return Boolean(readPendingOrderReview(item.order_id)); }
      catch (error) { return true; }
    });
    if (!rows.length) { body.innerHTML = '<tr><td colspan="7" class="muted">当前筛选下没有演示委托</td></tr>'; return; }
    body.innerHTML = rows.map((item) => {
      let pendingReview = null;
      try { pendingReview = readPendingOrderReview(item.order_id); } catch (error) { pendingReview = { unreadable: true }; }
      const actionable = ['submitted', 'needs_info'].includes(item.status);
      const actions = actionable
        ? `<div class="row"><button class="btn btn-secondary btn-sm" data-order-decision="needs_info" data-order-id="${escapeHtml(item.order_id)}" data-order-revision="${escapeHtml(item.revision ?? 1)}">补件</button><button class="btn btn-primary btn-sm" data-order-decision="approved" data-order-id="${escapeHtml(item.order_id)}" data-order-revision="${escapeHtml(item.revision ?? 1)}">批准</button><button class="btn btn-danger btn-sm" data-order-decision="rejected" data-order-id="${escapeHtml(item.order_id)}" data-order-revision="${escapeHtml(item.revision ?? 1)}">拒绝</button></div>`
        : '—';
      const retry = pendingReview
        ? `<button class="btn btn-warning btn-sm" data-order-review-retry="${escapeHtml(item.order_id)}" ${pendingReview.unreadable ? 'disabled' : ''}>${pendingReview.unreadable ? '重试记录不可读' : '恢复上次审核'}</button>`
        : '';
      const snapshot = item.template_snapshot || {};
      return `<tr><td>${escapeHtml(item.order_id)}</td><td>${escapeHtml(item.company_name)}<br><span class="muted">${escapeHtml(item.contact_name)}</span></td><td>${escapeHtml(item.project_name)}<br><span class="muted">${escapeHtml(item.sample_name)} · ${escapeHtml(item.quantity)}</span></td><td>${escapeHtml(snapshot.label || '—')}<br><span class="muted">${escapeHtml(snapshot.version || '')}</span></td><td><span class="pill">${escapeHtml(ORDER_STATUS_LABELS[item.status] || item.status)}</span></td><td>${escapeHtml(item.updated_at)}</td><td>${actions}${retry}</td></tr>`;
    }).join('');
  } catch (error) {
    body.innerHTML = `<tr><td colspan="7" class="muted">${escapeHtml(error.message || '演示委托服务暂不可用')}</td></tr>`;
  }
}
async function decideCustomerOrder(orderId, decision, expectedRevision = null) {
  const pendingKey = orderReviewPendingKey(orderId);
  let pending = null;
  try { pending = readPendingOrderReview(orderId); }
  catch (error) { toast('上次委托审核重试记录不可读取，已停止发送', 'error'); return; }
  if (!pending && !decision) { toast('本机没有待恢复的委托审核记录，请刷新队列', 'error'); return; }
  if (pending && (!pending.operationId || !pending.decision || !pending.reason || !Number.isInteger(pending.expectedRevision))) {
    toast('上次委托审核重试记录不完整，已停止发送', 'error');
    return;
  }
  if (pending && pending.unreadable) { toast('上次委托审核重试记录不可读取，已停止发送', 'error'); return; }
  if (pending && decision && pending.decision !== decision) {
    toast('上次审核结果尚未确认，请先恢复原操作', 'error');
    return;
  }
  if (!pending) {
    const label = ORDER_STATUS_LABELS[decision] || decision;
    const reason = window.prompt(`请填写“${label}”原因（必填）：`);
    if (reason === null) return;
    if (!reason.trim()) { toast('审核原因不能为空', 'error'); return; }
    try {
      if (!Number.isInteger(expectedRevision)) { toast('委托审核版本无效，请刷新队列', 'error'); return; }
      pending = {
        decision, reason: reason.trim(), expectedRevision,
        operationId: createSecureOperationId('order-review', window.crypto),
      };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
    } catch (error) { toast(error.message || '无法保存审核重试编号，未提交', 'error'); return; }
  }
  try {
    const result = await jsonFetch(`api/v1/customer-service/demo/admin/orders/${encodeURIComponent(orderId)}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.operationId },
      body: JSON.stringify({
        decision: pending.decision, reason: pending.reason,
        expected_revision: pending.expectedRevision,
      }),
    });
    try { sessionStorage.removeItem(pendingKey); } catch (error) { toast('审核已记录，但本机待办未能清除；请刷新恢复结果', 'info'); }
    toast(result.idempotent_replay ? '已恢复上次审核结果' : `已记录：${ORDER_STATUS_LABELS[pending.decision] || pending.decision}`);
    await loadCustomerOrders();
  } catch (error) {
    if (error.statusCode === 409) {
      try { sessionStorage.removeItem(pendingKey); } catch (storageError) { /* Retain if the browser storage is unavailable. */ }
      toast(`${error.message}；该操作未由本次重试写入，请刷新队列`, 'error');
    } else if (error.statusCode >= 400 && error.statusCode < 500) {
      try { sessionStorage.removeItem(pendingKey); } catch (storageError) { /* Preserve safe retry state if cleanup fails. */ }
      toast(error.message, 'error');
    } else toast(error.message, 'error');
  }
}
const CONVERSATION_STATUS_LABELS = { open: '待接手', assigned: '已接手', archived: '已归档' };
let customerConversationRequest = 0;
function conversationActionPendingKey(conversationId) {
  return `p0-demo-conversation-action:${String(state.me?.username || '')}:${conversationId}`;
}
function readPendingConversationAction(conversationId) {
  const value = sessionStorage.getItem(conversationActionPendingKey(conversationId));
  return value ? JSON.parse(value) : null;
}
async function loadCustomerConversations() {
  const body = $('conversationBody');
  if (!body) return;
  const requestId = ++customerConversationRequest;
  body.innerHTML = '<tr><td colspan="8" class="muted">正在读取本地演示会话…</td></tr>';
  const status = $('conversationStatusFilter').value;
  try {
    const result = await jsonFetch('api/v1/customer-service/demo/admin/conversations');
    if (requestId !== customerConversationRequest) return;
    const rows = result.conversations || [];
    state.customerConversations = rows;
    const companyQuery = $('conversationCompanyFilter').value.trim().toLocaleLowerCase();
    const priorityFilter = $('conversationPriorityFilter').value;
    const unreadFilter = $('conversationUnreadFilter').value;
    const visibleRows = filterReviewQueueItems.filterWithPendingOverride(rows, status, (item) => {
      if (companyQuery && !String(item.company_name || '').toLocaleLowerCase().includes(companyQuery)) return false;
      const isPriority = Boolean(item.priority_preview?.priority_candidate);
      if (priorityFilter === 'yes' && !isPriority) return false;
      if (priorityFilter === 'no' && isPriority) return false;
      const hasUnread = (Number(item.unread_count) || 0) > 0;
      if (unreadFilter === 'unread' && !hasUnread) return false;
      if (unreadFilter === 'read' && hasUnread) return false;
      return true;
    }, (item) => {
      try { return Boolean(readPendingConversationAction(item.conversation_id)); }
      catch (error) { return true; }
    });
    if (!visibleRows.length) { body.innerHTML = '<tr><td colspan="8" class="muted">当前组合筛选下没有会话</td></tr>'; return; }
    body.innerHTML = visibleRows.map((item) => {
      const last = (item.messages || []).slice(-1)[0] || {};
      const unread = Number(item.unread_count) || 0;
      const priority = item.priority_preview || {};
      const priorityLabel = priority.status !== 'candidate' ? '重点识别不可用' : '重点客户候选 · 人工核验';
      const companyCell = `<span>${escapeHtml(item.company_name)}</span>${priority.priority_candidate ? ` <span class="pill warn">${escapeHtml(priorityLabel)}</span>` : (priority.status === 'candidate' ? '' : ` <span class="pill">${escapeHtml(priorityLabel)}</span>`)}`;
      let pending = null;
      try { pending = readPendingConversationAction(item.conversation_id); } catch (error) { pending = { unreadable: true }; }
      const controls = `<div class="row"><button class="btn btn-secondary btn-sm" data-conversation-action="detail" data-conversation-id="${escapeHtml(item.conversation_id)}">查看</button>${pending ? `<button class="btn btn-warning btn-sm" data-conversation-action="retry" data-conversation-id="${escapeHtml(item.conversation_id)}" ${pending.unreadable ? 'disabled' : ''}>${pending.unreadable ? '重试记录不可读' : '恢复上次操作'}</button>` : item.status === 'archived' ? '' : `${item.status === 'open' ? `<button class="btn btn-secondary btn-sm" data-conversation-action="claim" data-conversation-id="${escapeHtml(item.conversation_id)}">接单</button>` : `<button class="btn btn-primary btn-sm" data-conversation-action="reply" data-conversation-id="${escapeHtml(item.conversation_id)}">回复</button>`}<button class="btn btn-secondary btn-sm" data-conversation-action="assign" data-conversation-id="${escapeHtml(item.conversation_id)}">转交</button><button class="btn btn-danger btn-sm" data-conversation-action="archive" data-conversation-id="${escapeHtml(item.conversation_id)}">归档</button>`}</div>`;
      return `<tr><td>${escapeHtml(item.conversation_id)}</td><td>${companyCell}</td><td>${escapeHtml(last.text || '—')}</td><td><span class="pill ${unread ? 'warn' : ''}">${unread ? `未读 ${unread}` : '已读'}</span></td><td><span class="pill">${escapeHtml(CONVERSATION_STATUS_LABELS[item.status] || item.status)}</span></td><td>${escapeHtml(item.assigned_to || '未接手')}</td><td>${escapeHtml(item.updated_at)}</td><td>${controls}</td></tr>`;
    }).join('');
  } catch (error) {
    if (requestId !== customerConversationRequest) return;
    body.innerHTML = `<tr><td colspan="8" class="muted">${escapeHtml(error.message || '会话演示服务暂不可用')}</td></tr>`;
  }
}
async function handleCustomerConversationAction(button) {
  const conversationId = button.dataset.conversationId;
  const action = button.dataset.conversationAction;
  if (action === 'reply') {
    const actor = String(state.me?.username || '');
    const pendingKey = `p0-demo-reply:${actor}:${conversationId}`;
    let pending = null;
    try {
      const stored = sessionStorage.getItem(pendingKey);
      if (stored) pending = JSON.parse(stored);
    } catch (error) { toast('上次回复重试记录不可读取，已停止发送', 'error'); return; }
    const text = window.prompt(
      pending ? '上次回复结果未确认。保持原文即可安全重试：' : '请输入回复内容（仅保存到本地演示会话，不会发送给客户微信）：',
      pending ? pending.text : '',
    );
    if (text === null) return;
    if (!text.trim()) { toast('回复内容不能为空', 'error'); return; }
    if (pending && pending.text !== text.trim()) { toast('上一条回复结果尚未确认，请先用原文重试', 'error'); return; }
    if (!pending) {
      try { pending = { text: text.trim(), operationId: createSecureOperationId('web', window.crypto) }; }
      catch (error) { toast(error.message, 'error'); return; }
      try { sessionStorage.setItem(pendingKey, JSON.stringify(pending)); }
      catch (error) { toast('无法保存回复重试编号，未提交回复', 'error'); return; }
    }
    try {
      const result = await jsonFetch(`api/v1/customer-service/demo/admin/conversations/${encodeURIComponent(conversationId)}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.operationId },
        body: JSON.stringify({ text: pending.text }),
      });
      sessionStorage.removeItem(pendingKey);
      toast(result.idempotent_replay ? '已恢复上次回复结果' : '回复已存入本地演示会话');
      await loadCustomerConversations();
    } catch (error) { toast(error.message, 'error'); }
    return;
  }
  if (action === 'detail') {
    const latest = await jsonFetch('api/v1/customer-service/demo/admin/conversations');
    const conversation = (latest.conversations || []).find((item) => item.conversation_id === conversationId);
    if (!conversation) { toast('会话已变化，请刷新队列', 'error'); return; }
    const history = (conversation.history || []).map((item) => `${item.at} · ${item.actor} · ${item.action} · ${item.from || '—'} → ${item.to} · ${item.reason || '—'}`);
    window.alert([`企业：${conversation.company_name}`, `状态：${CONVERSATION_STATUS_LABELS[conversation.status] || conversation.status}`, ...(conversation.messages || []).map((item) => `${item.sender === 'customer' ? '客户' : '客服'}：${item.text}`), '状态历史：', ...(history.length ? history : ['暂无'])].join('\n\n'));
    if ((Number(conversation.unread_count) || 0) > 0) {
      await jsonFetch(`api/v1/customer-service/demo/admin/conversations/${encodeURIComponent(conversationId)}/read`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ through_message_count: conversation.message_count }),
      });
      await loadCustomerConversations();
    }
    return;
  }
  const pendingKey = conversationActionPendingKey(conversationId);
  const actor = String(state.me?.username || '');
  if (!actor) { toast('当前管理员身份不可用，未提交操作', 'error'); return; }
  let pending = null;
  try { pending = readPendingConversationAction(conversationId); }
  catch (error) { toast('上次队列操作记录不可读取，已停止发送', 'error'); return; }
  if (pending && (pending.unreadable || !pending.operationId || !pending.action || !pending.payload
      || pending.fingerprint !== JSON.stringify({ action: pending.action, payload: pending.payload }))) {
    toast('上次队列操作记录不完整，已停止发送', 'error'); return;
  }
  if (action === 'retry') {
    if (!pending) { toast('本机没有待恢复操作，请刷新队列', 'error'); return; }
  } else if (pending && pending.action !== action) {
    toast('上次操作结果尚未确认，请先恢复原操作', 'error'); return;
  }
  let route = pending ? pending.action : action;
  let body = pending ? pending.payload : {};
  if (!pending) {
    const reason = window.prompt(`请填写${action === 'claim' ? '接单' : action === 'assign' ? '转交' : '归档'}原因（必填）：`);
    if (reason === null) return;
    if (!reason.trim()) { toast('原因不能为空', 'error'); return; }
    body = { reason: reason.trim() };
    if (action === 'assign') {
      const assignee = window.prompt('请输入启用中的管理员账号（经理角色映射尚未配置）：');
      if (assignee === null) return;
      if (!assignee.trim()) { toast('转交对象不能为空', 'error'); return; }
      body.assignee = assignee.trim();
    }
    try {
      pending = {
        action: route, payload: body,
        fingerprint: JSON.stringify({ action: route, payload: body }),
        operationId: createSecureOperationId('web', window.crypto),
      };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
    } catch (error) { toast(error.message || '无法保存队列操作重试编号，未提交', 'error'); return; }
  }
  try {
    const result = await jsonFetch(`api/v1/customer-service/demo/admin/conversations/${encodeURIComponent(conversationId)}/${route}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.operationId },
      body: JSON.stringify(pending.payload),
    });
    try { sessionStorage.removeItem(pendingKey); }
    catch (error) { toast('操作已记录，但本机待办未能清除；刷新后可恢复结果', 'info'); }
    toast(result.idempotent_replay ? '已恢复上次队列操作' : (route === 'claim' ? '会话已接手' : route === 'assign' ? '会话已转交' : '会话已归档'));
    await loadCustomerConversations();
  } catch (error) {
    if (isDefinitiveReviewActionRejection(error.statusCode)) {
      try { sessionStorage.removeItem(pendingKey); } catch (storageError) { /* retain if cleanup failed */ }
    }
    await loadCustomerConversations();
    toast(error.message, 'error');
  }
}
const QUOTE_INQUIRY_STATUS_LABELS = {
  draft: '草稿', submitted: '已提交', need_info: '待补信息', preview: '内部预览',
  needs_human: '待人工处理', resolved: '已关闭',
};
function quoteInquiryActionPendingKey(inquiryId) {
  return `p0-quote-inquiry-action:${String(state.me?.username || '')}:${inquiryId}`;
}
function readPendingQuoteInquiryAction(inquiryId) {
  const value = sessionStorage.getItem(quoteInquiryActionPendingKey(inquiryId));
  return value ? JSON.parse(value) : null;
}
function collectQuoteInquiryPayload() {
  const project_name = $('inquiryProject').value.trim();
  if (!project_name) throw new Error('请填写已授权的项目标识');
  const quantityText = $('inquiryQuantity').value.trim();
  const quantity = quantityText ? Number(quantityText) : null;
  if (quantity !== null && (!Number.isFinite(quantity) || quantity <= 0)) throw new Error('数量必须大于 0');
  return {
    project_name,
    context: $('inquiryContext').value.trim(),
    lines: [{
      item_name: $('inquiryItem').value.trim(),
      sample_name: $('inquirySample').value.trim(),
      standard: $('inquiryStandard').value.trim(),
      quantity,
      unit: $('inquiryUnit').value.trim(),
    }],
  };
}
function restoreQuoteInquiryPayload(payload) {
  const line = (payload && Array.isArray(payload.lines) ? payload.lines : [])[0] || {};
  $('inquiryProject').value = payload.project_name || '';
  $('inquiryContext').value = payload.context || '';
  $('inquiryItem').value = line.item_name || '';
  $('inquirySample').value = line.sample_name || '';
  $('inquiryStandard').value = line.standard || '';
  $('inquiryQuantity').value = line.quantity == null ? '' : String(line.quantity);
  $('inquiryUnit').value = line.unit || '';
}
function renderQuoteInquiryPreview(inquiry) {
  const target = $('inquiryPreview');
  const preview = inquiry.preview;
  const rows = preview && Array.isArray(preview.lines) ? preview.lines : [];
  const facts = rows.map((line) => {
    const request = line.request || {};
    const price = line.price_preview ? `${line.price_preview.unit_price} / 单位` : '未匹配';
    const sources = (line.source_refs || []).map((source) => source.record_id).join('、') || '无来源记录';
    return `<li><b>${escapeHtml(request.item_name || '待补项目')}</b> · ${escapeHtml(QUOTE_INQUIRY_STATUS_LABELS[line.action] || line.action)}<br><span class="muted">内部单价事实：${escapeHtml(price)}；来源：${escapeHtml(sources)}；总价不计算。</span><br><span class="muted">待处理：${escapeHtml((line.missing_fields || []).join('、') || '无')}</span></li>`;
  }).join('');
  target.classList.remove('hidden');
  target.innerHTML = `<div class="banner ${inquiry.status === 'needs_human' ? 'warn' : 'info'}">状态：${escapeHtml(QUOTE_INQUIRY_STATUS_LABELS[inquiry.status] || inquiry.status)}${inquiry.manual_reason ? ` · ${escapeHtml(inquiry.manual_reason)}` : ''}</div>${facts ? `<ul class="check-list mt-16">${facts}</ul>` : '<p class="muted mt-16">尚无 CRM 来源预览；询价已保留供人工处理。</p>'}<p class="muted">${escapeHtml((preview && preview.warnings || []).join('；'))}</p>`;
}
function renderMyQuoteInquiries() {
  const target = $('myQuoteInquiries');
  if (!state.myQuoteInquiries.length) {
    target.innerHTML = '<div class="muted">还没有询价工单</div>';
    return;
  }
  target.innerHTML = state.myQuoteInquiries.map((item) => `
    <button type="button" class="history-item" data-my-inquiry="${escapeHtml(item.inquiry_id)}">
      <span><b>${escapeHtml(item.project_name)}</b><small>${escapeHtml(item.inquiry_id)} · ${escapeHtml(item.updated_at)}</small></span>
      <span class="pill">${escapeHtml(QUOTE_INQUIRY_STATUS_LABELS[item.status] || item.status)}</span>
    </button>`).join('');
}
async function loadMyQuoteInquiries() {
  const target = $('myQuoteInquiries');
  if (!target) return;
  try {
    const result = await jsonFetch('api/v1/customer-service/quote-inquiries');
    state.myQuoteInquiries = result.inquiries || [];
    renderMyQuoteInquiries();
  } catch (error) {
    target.innerHTML = `<div class="muted">${escapeHtml(error.message || '询价工单暂不可用')}；服务端需启用本地询价流程。</div>`;
  }
}
async function saveQuoteInquiryDraft() {
  const payload = collectQuoteInquiryPayload();
  let result;
  if (state.quoteInquiryId) {
    const current = state.myQuoteInquiries.find((item) => item.inquiry_id === state.quoteInquiryId);
    if (current && !['draft', 'need_info', 'needs_human'].includes(current.status)) {
      throw new Error('当前工单状态不可修改；请新建询价或联系人工处理');
    }
    result = await jsonFetch(`api/v1/customer-service/quote-inquiries/${encodeURIComponent(state.quoteInquiryId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
  } else {
    const actor = String(state.me?.username || '');
    if (!actor) throw new Error('登录状态不可用，未创建询价草稿');
    const pendingKey = `p0-quote-inquiry-create:${actor}`;
    let pending = null;
    try {
      const stored = sessionStorage.getItem(pendingKey);
      if (stored) pending = JSON.parse(stored);
    } catch (error) { throw new Error('询价创建重试记录不可读取，已停止发送'); }
    const payloadFingerprint = JSON.stringify(payload);
    if (pending && (!pending.operationId || !pending.payload || !pending.fingerprint)) {
      throw new Error('询价创建重试记录不完整，已停止发送');
    }
    if (pending && pending.confirmedInquiryId && pending.fingerprint !== payloadFingerprint) {
      try { sessionStorage.removeItem(pendingKey); }
      catch (error) { throw new Error('上一条询价已确认，但本机记录无法清理；请稍后重试'); }
      pending = null;
    }
    if (pending && pending.fingerprint !== payloadFingerprint) {
      restoreQuoteInquiryPayload(pending.payload);
      throw new Error('上次询价创建结果尚未确认；已恢复原资料，请按原内容重试');
    }
    if (!pending) {
      pending = { payload, fingerprint: payloadFingerprint, operationId: createSecureOperationId('quote', window.crypto) };
      try { sessionStorage.setItem(pendingKey, JSON.stringify(pending)); }
      catch (error) { throw new Error('无法保存询价重试编号，未提交草稿'); }
    }
    try {
      result = await jsonFetch('api/v1/customer-service/quote-inquiries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.operationId },
        body: JSON.stringify(pending.payload),
      });
    } catch (error) {
      if (error.statusCode >= 400 && error.statusCode < 500 && error.statusCode !== 409) {
        try { sessionStorage.removeItem(pendingKey); } catch (storageError) { /* keep uncertain retry state */ }
      }
      throw error;
    }
    pending.confirmedInquiryId = result.inquiry.inquiry_id;
    try {
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      sessionStorage.removeItem(pendingKey);
    } catch (error) {
      toast('草稿已保存；本机重试记录未能清理，新建询价时将先回读并确认该工单', 'info');
    }
  }
  const inquiry = result.inquiry;
  state.quoteInquiryId = inquiry.inquiry_id;
  renderQuoteInquiryPreview(inquiry);
  await loadMyQuoteInquiries();
  return inquiry;
}
async function submitQuoteInquiry() {
  const button = $('inquirySubmit');
  busy(button, true, '提交中…');
  try {
    if (state.quoteInquiryId) {
      const latest = await jsonFetch(`api/v1/customer-service/quote-inquiries/${encodeURIComponent(state.quoteInquiryId)}`);
      if (latest.inquiry.status !== 'draft') {
        const inquiry = latest.inquiry;
        renderQuoteInquiryPreview(inquiry);
        $('inquiryStatus').textContent = `工单 ${inquiry.inquiry_id} · ${QUOTE_INQUIRY_STATUS_LABELS[inquiry.status] || inquiry.status}`;
        toast('已恢复工单当前状态');
        await loadMyQuoteInquiries();
        return;
      }
    }
    const draft = await saveQuoteInquiryDraft();
    if (draft.status !== 'draft') {
      renderQuoteInquiryPreview(draft);
      $('inquiryStatus').textContent = `工单 ${draft.inquiry_id} · ${QUOTE_INQUIRY_STATUS_LABELS[draft.status] || draft.status}`;
      toast('已恢复工单当前状态');
      await loadMyQuoteInquiries();
      return;
    }
    const result = await jsonFetch(`api/v1/customer-service/quote-inquiries/${encodeURIComponent(draft.inquiry_id)}/submit`, { method: 'POST' });
    renderQuoteInquiryPreview(result.inquiry);
    $('inquiryStatus').textContent = `工单 ${result.inquiry.inquiry_id} · ${QUOTE_INQUIRY_STATUS_LABELS[result.inquiry.status] || result.inquiry.status}`;
    toast('询价已保存，按来源状态转入内部处理');
    await loadMyQuoteInquiries();
  } catch (error) {
    $('inquiryStatus').textContent = error.message;
    toast(error.message, 'error');
  } finally {
    busy(button, false);
  }
}
async function openMyQuoteInquiry(inquiryId) {
  try {
    const result = await jsonFetch(`api/v1/customer-service/quote-inquiries/${encodeURIComponent(inquiryId)}`);
    const inquiry = result.inquiry;
    state.quoteInquiryId = inquiry.inquiry_id;
    $('inquiryProject').value = inquiry.project_name || '';
    $('inquiryContext').value = inquiry.context || '';
    const line = (inquiry.lines || [])[0] || {};
    $('inquiryItem').value = line.item_name || '';
    $('inquirySample').value = line.sample_name || '';
    $('inquiryStandard').value = line.standard || '';
    $('inquiryQuantity').value = line.quantity === null ? '' : (line.quantity || '');
    $('inquiryUnit').value = line.unit || '';
    $('inquiryStatus').textContent = `${inquiry.inquiry_id} · ${QUOTE_INQUIRY_STATUS_LABELS[inquiry.status] || inquiry.status}`;
    renderQuoteInquiryPreview(inquiry);
  } catch (error) {
    toast(error.message, 'error');
  }
}
async function loadCustomerQuoteInquiries() {
  const body = $('quoteInquiryBody');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="8" class="muted">正在读取询价队列…</td></tr>';
  const status = $('quoteInquiryStatusFilter').value;
  try {
    const result = await jsonFetch('api/v1/customer-service/quote-inquiries/admin/queue');
    state.customerQuoteInquiries = filterReviewQueueItems(result.inquiries || [], status, (item) => {
      try { return Boolean(readPendingQuoteInquiryAction(item.inquiry_id)); }
      catch (error) { return true; }
    });
    if (!state.customerQuoteInquiries.length) {
      body.innerHTML = '<tr><td colspan="8" class="muted">当前筛选下没有询价工单</td></tr>';
      return;
    }
    body.innerHTML = state.customerQuoteInquiries.map((item) => {
      let pendingAction = null;
      try { pendingAction = readPendingQuoteInquiryAction(item.inquiry_id); }
      catch (error) { pendingAction = { unreadable: true }; }
      const active = ['need_info', 'preview', 'needs_human'].includes(item.status);
      const lineSummary = (item.lines || []).map((line) => line.item_name || '待补项目').join('、');
      const actions = `${active
        ? `<div class="row">${item.assigned_to ? '' : `<button class="btn btn-secondary btn-sm" data-inquiry-action="claim" data-inquiry-id="${escapeHtml(item.inquiry_id)}">接单</button>`}<button class="btn btn-secondary btn-sm" data-inquiry-action="assign" data-inquiry-id="${escapeHtml(item.inquiry_id)}">转派</button><button class="btn btn-danger btn-sm" data-inquiry-action="resolve" data-inquiry-id="${escapeHtml(item.inquiry_id)}">关闭</button></div>`
        : '—'}${pendingAction ? `<button class="btn btn-warning btn-sm" data-inquiry-action="retry" data-inquiry-id="${escapeHtml(item.inquiry_id)}" ${pendingAction.unreadable ? 'disabled' : ''}>${pendingAction.unreadable ? '重试记录不可读' : '恢复上次操作'}</button>` : ''}`;
      return `<tr><td><button class="btn btn-secondary btn-sm" data-inquiry-action="detail" data-inquiry-id="${escapeHtml(item.inquiry_id)}">${escapeHtml(item.inquiry_id)}</button></td><td>${escapeHtml(item.project_name)}</td><td>${escapeHtml(item.created_by)}</td><td>${escapeHtml(lineSummary || '—')}</td><td><span class="pill">${escapeHtml(QUOTE_INQUIRY_STATUS_LABELS[item.status] || item.status)}</span></td><td>${escapeHtml(item.assigned_to || '未接单')}</td><td>${escapeHtml(item.updated_at)}</td><td>${actions}</td></tr>`;
    }).join('');
  } catch (error) {
    body.innerHTML = `<tr><td colspan="8" class="muted">${escapeHtml(error.message || '询价队列暂不可用')}</td></tr>`;
  }
}
async function handleCustomerQuoteInquiryAction(button) {
  const inquiryId = button.dataset.inquiryId;
  const action = button.dataset.inquiryAction;
  if (action === 'detail') {
    const item = state.customerQuoteInquiries.find((row) => row.inquiry_id === inquiryId);
    if (!item) return;
    const lines = (item.preview && item.preview.lines || []).map((line) => {
      const price = line.price_preview ? `${line.price_preview.unit_price}（内部事实；未计算总价）` : '未匹配';
      return `${line.request.item_name || '待补项目'}｜${line.action}｜${price}`;
    }).join('\n');
    window.alert([`项目：${item.project_name}`, `上下文：${item.context || '—'}`, `状态原因：${item.manual_reason || '—'}`, `明细：\n${lines || '尚无 CRM 预览'}`].join('\n\n'));
    return;
  }
  const pendingKey = quoteInquiryActionPendingKey(inquiryId);
  let pending = null;
  try { pending = readPendingQuoteInquiryAction(inquiryId); }
  catch (error) { toast('上次询价队列操作记录不可读取，已停止发送', 'error'); return; }
  if (pending && pending.unreadable) { toast('上次询价队列操作记录不可读取，已停止发送', 'error'); return; }
  if (pending && (!pending.operationId || !pending.action || !pending.payload
      || pending.fingerprint !== JSON.stringify({ action: pending.action, payload: pending.payload }))) {
    toast('上次询价队列操作记录不完整，已停止发送', 'error'); return;
  }
  if (action === 'retry') {
    if (!pending) { toast('本机没有待恢复的操作，请刷新队列', 'error'); return; }
  } else if (pending && pending.action !== action) {
    toast('上次操作结果尚未确认，请先恢复原操作', 'error'); return;
  }
  let body = pending ? pending.payload : {};
  let route = pending ? pending.action : action;
  if (!pending && (action === 'claim' || action === 'resolve')) {
    const reason = window.prompt(`请填写${action === 'claim' ? '接单' : '关闭'}原因（必填）：`);
    if (reason === null) return;
    if (!reason.trim()) { toast('原因不能为空', 'error'); return; }
    body.reason = reason.trim();
  } else if (!pending && action === 'assign') {
    const assignee = window.prompt('请输入启用中的销售账号：');
    if (assignee === null) return;
    const reason = window.prompt('请填写转派原因（必填）：');
    if (reason === null) return;
    if (!assignee.trim() || !reason.trim()) { toast('转派对象和原因不能为空', 'error'); return; }
    body = { assignee: assignee.trim(), reason: reason.trim() };
  } else if (!pending) return;
  if (!pending) {
    const actor = String(state.me?.username || '');
    if (!actor) { toast('当前员工身份不可用，未提交操作', 'error'); return; }
    try {
      pending = {
        action: route, payload: body,
        fingerprint: JSON.stringify({ action: route, payload: body }),
        operationId: createSecureOperationId('quote-admin', window.crypto),
      };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
    } catch (error) { toast(error.message || '无法保存操作重试编号，未提交', 'error'); return; }
  }
  try {
    const result = await jsonFetch(`api/v1/customer-service/quote-inquiries/admin/${encodeURIComponent(inquiryId)}/${route}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pending.operationId },
      body: JSON.stringify(pending.payload),
    });
    try { sessionStorage.removeItem(pendingKey); }
    catch (error) { toast('操作已记录，但本机重试记录无法清理；请刷新恢复状态', 'info'); }
    toast(result.idempotent_replay ? '已恢复上次操作结果' : (route === 'claim' ? '已接单' : route === 'assign' ? '已转派' : '工单已关闭'));
    await loadCustomerQuoteInquiries();
  } catch (error) {
    if (isDefinitiveReviewActionRejection(error.statusCode)) {
      try { sessionStorage.removeItem(pendingKey); } catch (storageError) { /* Keep the operation if cleanup fails. */ }
      await loadCustomerQuoteInquiries();
    }
    toast(error.message, 'error');
  }
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
  $('projectSub').textContent = `设置 ${username} 可访问的项目；留空时无项目访问权限，输入 * 才表示全部项目。`;
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
  $('cfgBizWhitelistKeywords').value = (business.whitelist?.keywords || []).join('\n');
  const regionRouting = business.region_routing || {};
  $('cfgBizRegionManagers').value = (regionRouting.region_managers || []).map((item) => `${item.region}=${item.manager_id}`).join('\n');
  $('cfgBizBackupManager').value = regionRouting.backup_manager_id || '';
  $('cfgBizRegionRoutingEnabled').checked = Boolean(regionRouting.enabled);
  const canManageRegionRouting = Boolean(state.me && state.me.role === 'admin');
  $('cfgBizRegionManagers').closest('.field').classList.toggle('hidden', !canManageRegionRouting);
  $('cfgBizBackupManager').closest('.field').classList.toggle('hidden', !canManageRegionRouting);
  const canManageCustomerLists = Boolean(state.me && state.me.role === 'admin');
  ['cfgBizBlacklist', 'cfgBizWhitelist', 'cfgBizWhitelistKeywords'].forEach((id) => {
    $(id).closest('.field').classList.toggle('hidden', !canManageCustomerLists);
  });

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
    expected_revision: state.config?.config?.revision,
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
    ...(state.me && state.me.role === 'admin' ? {
      blacklist: { companies: splitList($('cfgBizBlacklist').value) },
      whitelist: {
        companies: splitList($('cfgBizWhitelist').value),
        keywords: splitList($('cfgBizWhitelistKeywords').value),
      },
    } : {}),
    ...(state.me && state.me.role === 'admin' ? { region_routing: {
      enabled: $('cfgBizRegionRoutingEnabled').checked,
      region_managers: parseRegionManagers($('cfgBizRegionManagers').value),
      backup_manager_id: $('cfgBizBackupManager').value.trim(),
    } } : {}),
  };
}
function parseRegionManagers(value) {
  return String(value || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split('=');
    if (parts.length !== 2 || !parts[0].trim() || !parts[1].trim()) {
      throw new Error(`地区经理映射格式不正确：${line}（应为「地区=账号」）`);
    }
    return { region: parts[0].trim(), manager_id: parts[1].trim() };
  });
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
    const revision = state.config?.config?.revision;
    const query = Number.isInteger(revision) ? `?expected_revision=${encodeURIComponent(revision)}` : '';
    state.config = await jsonFetch(`api/v1/admin/config/${section}/reset${query}`, { method: 'POST' });
    renderAdminConfig();
    await loadModuleConfig();
    toast(`已恢复默认${label}`);
  } catch (error) {
    showError(error);
  }
}
async function importBusinessBlacklist() {
  const input = $('cfgBizBlacklistFile');
  const file = input.files && input.files[0];
  if (!file) { toast('请选择黑名单 CSV 文件', 'error'); return; }
  if (file.size > 200_000) { toast('CSV 文件不能超过 200 KB', 'error'); return; }
  if ($('cfgBizBlacklistImportMode').value === 'replace') {
    const confirmed = await confirmDialog({
      title: '替换现有黑名单？',
      message: 'CSV 校验通过后将以文件中的企业名单替换当前名单。',
      confirmText: '替换名单', tone: 'danger',
    });
    if (!confirmed) return;
  }
  const button = $('cfgBizBlacklistImport');
  busy(button, true, '导入中…');
  try {
    const csvText = await file.text();
    const result = await jsonFetch('api/v1/admin/config/business/blacklist/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        csv_text: csvText,
        mode: $('cfgBizBlacklistImportMode').value,
        expected_revision: state.config?.config?.revision,
      }),
    });
    state.config = result;
    renderAdminConfig();
    await loadModuleConfig();
    const summary = result.blacklist_import || {};
    toast(`黑名单已导入：新增 ${summary.added || 0} 家，当前共 ${summary.company_count || 0} 家`);
  } catch (error) {
    showError(error);
  } finally {
    input.value = '';
    busy(button, false);
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
async function loadCrmSchedules() {
  const body = $('crmScheduleBody');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="6" class="muted">正在读取本地排期库…</td></tr>';
  try {
    const result = await jsonFetch('api/v1/crm/base-data/schedules');
    crmScheduleRows = Array.isArray(result.schedules) ? result.schedules : [];
    $('crmScheduleVersion').textContent = `版本 ${Number(result.version) || 0} · ${crmScheduleRows.length} 条排期`;
    if (!crmScheduleRows.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">暂无本地排期记录</td></tr>';
      return;
    }
    body.innerHTML = crmScheduleRows.map((row, index) => `<tr>
      <td>${escapeHtml(row.project_id || '—')}</td>
      <td>${escapeHtml(row.project_name || '—')}</td>
      <td>${escapeHtml(row.start_date || '未提供')} – ${escapeHtml(row.end_date || '未提供')}</td>
      <td>${escapeHtml(row.status || '未填写')}<br><small class="muted">状态语义待客户确认</small></td>
      <td>${escapeHtml(row.note || '—')}</td>
      <td>${can('schedule:manage') || can('crm:base:manage') ? `<button type="button" class="btn btn-danger btn-sm" data-crm-schedule-delete="${index}">删除</button>` : '只读'}</td>
    </tr>`).join('');
  } catch (error) {
    crmScheduleRows = [];
    body.innerHTML = `<tr><td colspan="6" class="muted">${escapeHtml(error.message || '排期库暂不可用')}</td></tr>`;
  }
}

async function loadCrmQualifications() {
  const body = $('crmQualificationBody');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="6" class="muted">正在读取本地资质库…</td></tr>';
  try {
    const result = await jsonFetch('api/v1/crm/base-data');
    crmQualificationRows = Array.isArray(result.qualifications) ? result.qualifications : [];
    $('crmQualificationVersion').textContent = `版本 ${Number(result.version) || 0} · ${crmQualificationRows.length} 条资质`;
    if (!crmQualificationRows.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">暂无本地资质记录</td></tr>';
      return;
    }
    body.innerHTML = crmQualificationRows.map((row, index) => `<tr>
      <td>${escapeHtml(row.name || '—')}</td>
      <td>${escapeHtml(row.certificate_no || '—')}</td>
      <td>${escapeHtml(row.scope || '未提供')}</td>
      <td>${escapeHtml(row.valid_until || '未提供')}</td>
      <td>${escapeHtml(row.status || '未填写')}<br><small class="muted">状态语义待客户确认</small></td>
      <td><button type="button" class="btn btn-danger btn-sm" data-crm-qualification-delete="${index}">删除</button></td>
    </tr>`).join('');
  } catch (error) {
    crmQualificationRows = [];
    body.innerHTML = `<tr><td colspan="6" class="muted">${escapeHtml(error.message || '资质库暂不可用')}</td></tr>`;
  }
}

async function saveCrmQualification(event) {
  event.preventDefault();
  const button = $('crmQualificationSave');
  const payload = {
    name: $('crmQualificationName').value.trim(),
    certificate_no: $('crmQualificationCertificate').value.trim(),
    scope: $('crmQualificationScope').value.trim(),
    valid_until: $('crmQualificationValidUntil').value,
    status: $('crmQualificationStatus').value.trim(),
    source: $('crmQualificationSource').value.trim(),
    reason: $('crmQualificationReason').value.trim(),
  };
  if (!payload.name || !payload.source || !payload.reason) {
    toast('请填写资质名称、来源和变更原因', 'error');
    return;
  }
  busy(button, true, '保存中…');
  try {
    let result;
    try {
      result = await jsonFetch('api/v1/crm/base-data/qualifications', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    } catch (error) {
      if (error.statusCode !== 409 || !window.confirm('相同证书号或资质名称已有记录。确认覆盖该条记录吗？')) throw error;
      result = await jsonFetch('api/v1/crm/base-data/qualifications', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, force_replace: true }),
      });
    }
    $('crmQualificationForm').reset();
    toast(result.action === 'updated' ? '资质记录已更新并记录审计' : '资质记录已新增并记录审计');
    await loadCrmQualifications();
  } catch (error) {
    toast(`${error.message || '资质保存失败'}。请刷新列表核对服务端状态后再操作。`, 'error');
  } finally {
    busy(button, false);
  }
}

async function deleteCrmQualification(index) {
  const row = crmQualificationRows[index];
  if (!row) return;
  if (!window.confirm(`确定删除资质“${row.name}”${row.certificate_no ? `（${row.certificate_no}）` : ''}？`)) return;
  const reason = window.prompt('请输入删除原因');
  if (!reason || !reason.trim()) return;
  const source = window.prompt('请输入来源或变更记录');
  if (!source || !source.trim()) {
    toast('请填写删除来源', 'error');
    return;
  }
  try {
    await jsonFetch('api/v1/crm/base-data/qualifications', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...row, source: source.trim(), reason: reason.trim() }),
    });
    toast('资质记录已删除并记录审计');
    await loadCrmQualifications();
  } catch (error) {
    toast(`${error.message || '资质删除失败'}。请刷新列表核对服务端状态。`, 'error');
  }
}

function baseDataErrorRow(body, count, message) {
  body.innerHTML = `<tr><td colspan="${count}" class="muted">${escapeHtml(message)}</td></tr>`;
}

async function loadCrmPrices() {
  const body = $('crmPriceBody');
  if (!body) return;
  baseDataErrorRow(body, 7, '正在读取本地价格库…');
  try {
    const result = await jsonFetch('api/v1/crm/base-data');
    crmPriceRows = Array.isArray(result.prices) ? result.prices : [];
    $('crmPriceVersion').textContent = `版本 ${Number(result.version) || 0} · ${crmPriceRows.length} 条价格`;
    if (!crmPriceRows.length) { baseDataErrorRow(body, 7, '暂无本地价格记录'); return; }
    body.innerHTML = crmPriceRows.map((row, index) => `<tr>
      <td>${escapeHtml(row.project_name || '—')}<br><small class="muted">${escapeHtml(row.project_id || '项目编号未提供')}</small></td>
      <td>${escapeHtml(row.sample_name || '未提供样品')}<br><small>${escapeHtml(row.item_name || '—')}</small></td>
      <td>${escapeHtml(row.standard || '未提供标准')}<br><small>${escapeHtml(row.department || '未提供部门')}</small></td>
      <td>${escapeHtml(String(row.quantity ?? '—'))} / ${escapeHtml(row.unit || '未提供')}</td>
      <td>${escapeHtml(String(row.unit_price ?? '—'))} / ${escapeHtml(String(row.discount ?? '—'))}%</td>
      <td>${escapeHtml(row.valid_until || '未提供')}</td>
      <td>${can('crm:prices:manage') || can('crm:base:manage') ? `<button type="button" class="btn btn-danger btn-sm" data-crm-price-delete="${index}">删除</button>` : '只读'}</td>
    </tr>`).join('');
  } catch (error) {
    crmPriceRows = [];
    baseDataErrorRow(body, 7, error.message || '价格库暂不可用');
  }
}

async function saveCrmPrice(event) {
  event.preventDefault();
  const button = $('crmPriceSave');
  let numericValues;
  try {
    numericValues = window.CrmPriceInput.preserveNumericText(
      $('crmPriceQuantity').value, $('crmPriceUnitPrice').value, $('crmPriceDiscount').value,
    );
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  const payload = {
    project_id: $('crmPriceProjectId').value.trim(),
    project_name: $('crmPriceProjectName').value.trim(),
    sample_name: $('crmPriceSample').value.trim(),
    item_name: $('crmPriceItem').value.trim(),
    standard: $('crmPriceStandard').value.trim(),
    department: $('crmPriceDepartment').value.trim(),
    unit: $('crmPriceUnit').value.trim(),
    // Preserve the decimal strings; server-side checks detect underflow/loss.
    ...numericValues,
    valid_until: $('crmPriceValidUntil').value,
    source: $('crmPriceSource').value.trim(),
    reason: $('crmPriceReason').value.trim(),
  };
  if (!payload.project_name || !payload.item_name || !payload.source || !payload.reason) {
    toast('请检查项目、试验项目、有效数值、来源和变更原因', 'error');
    return;
  }
  busy(button, true, '保存中…');
  try {
    let result;
    try {
      result = await jsonFetch('api/v1/crm/base-data/prices', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    } catch (error) {
      if (error.statusCode !== 409 || !window.confirm('相同项目、样品、试验项目和标准已有记录。确认覆盖该条记录吗？')) throw error;
      result = await jsonFetch('api/v1/crm/base-data/prices', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, force_replace: true }),
      });
    }
    $('crmPriceForm').reset();
    toast(result.action === 'updated' ? '价格记录已更新并记录审计' : '价格记录已新增并记录审计');
    await loadCrmPrices();
  } catch (error) {
    toast(`${error.message || '价格保存失败'}。请刷新列表核对服务端状态后再操作。`, 'error');
  } finally {
    busy(button, false);
  }
}

async function deleteCrmPrice(index) {
  const row = crmPriceRows[index];
  if (!row) return;
  if (!window.confirm(`确定删除“${row.project_name} / ${row.item_name}”价格记录？`)) return;
  const reason = window.prompt('请输入删除原因');
  if (!reason || !reason.trim()) return;
  const source = window.prompt('请输入来源或变更记录');
  if (!source || !source.trim()) { toast('请填写删除来源', 'error'); return; }
  try {
    await jsonFetch('api/v1/crm/base-data/prices', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...row, source: source.trim(), reason: reason.trim() }),
    });
    toast('价格记录已删除并记录审计');
    await loadCrmPrices();
  } catch (error) {
    toast(`${error.message || '价格删除失败'}。请刷新列表核对服务端状态。`, 'error');
  }
}

async function loadCrmNameMappings() {
  const body = $('crmNameMappingBody');
  if (!body) return;
  baseDataErrorRow(body, 6, '正在读取本地名称映射…');
  try {
    const result = await jsonFetch('api/v1/crm/base-data');
    crmNameMappingRows = Array.isArray(result.name_mappings) ? result.name_mappings : [];
    $('crmNameMappingVersion').textContent = `版本 ${Number(result.version) || 0} · ${crmNameMappingRows.length} 条映射`;
    if (!crmNameMappingRows.length) { baseDataErrorRow(body, 6, '暂无名称映射'); return; }
    body.innerHTML = crmNameMappingRows.map((row, index) => {
      const approved = row.review_status === 'approved';
      const status = approved
        ? `已批准${row.approved_by ? ` · ${escapeHtml(row.approved_by)}` : ''}`
        : `待审批${row.review_status && row.review_status !== 'pending' ? ` · ${escapeHtml(row.review_status)}` : ''}`;
      return `<tr><td>${escapeHtml(row.alias || '—')}</td><td>${escapeHtml(row.canonical || '—')}</td>
        <td>${escapeHtml(row.note || '—')}</td><td>${status}</td><td>${escapeHtml(row.source || '按创建记录查看')}</td><td>
        ${approved ? '' : `<button type="button" class="btn btn-secondary btn-sm" data-crm-name-mapping-approve="${index}">批准</button> `}
        <button type="button" class="btn btn-danger btn-sm" data-crm-name-mapping-delete="${index}">删除</button></td></tr>`;
    }).join('');
  } catch (error) {
    crmNameMappingRows = [];
    baseDataErrorRow(body, 6, error.message || '名称映射暂不可用');
  }
}

async function saveCrmNameMapping(event) {
  event.preventDefault();
  const button = $('crmNameMappingSave');
  const payload = {
    alias: $('crmNameMappingAlias').value.trim(),
    canonical: $('crmNameMappingCanonical').value.trim(),
    note: $('crmNameMappingNote').value.trim(),
    source: $('crmNameMappingSource').value.trim(),
    reason: $('crmNameMappingReason').value.trim(),
  };
  if (!payload.alias || !payload.canonical || !payload.source || !payload.reason) {
    toast('请填写来源名称、标准名称、来源和变更原因', 'error'); return;
  }
  busy(button, true, '保存中…');
  try {
    let result;
    try {
      result = await jsonFetch('api/v1/crm/base-data/name_mappings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    } catch (error) {
      if (error.statusCode !== 409 || !window.confirm('该来源名称已有映射。确认用新内容替换并重新进入待审批状态吗？')) throw error;
      result = await jsonFetch('api/v1/crm/base-data/name_mappings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, force_replace: true }),
      });
    }
    $('crmNameMappingForm').reset();
    toast(result.action === 'updated' ? '映射已替换并回到待审批状态' : '映射已保存，等待管理员审批');
    await loadCrmNameMappings();
  } catch (error) {
    toast(`${error.message || '映射保存失败'}。请刷新列表核对服务端状态后再操作。`, 'error');
  } finally {
    busy(button, false);
  }
}

async function approveCrmNameMapping(index) {
  const row = crmNameMappingRows[index];
  if (!row || row.review_status === 'approved') return;
  if (!window.confirm(`确认批准“${row.alias}”→“${row.canonical}”？批准后该映射可能参与项目名称匹配。`)) return;
  const source = window.prompt('请输入审批依据/来源');
  if (!source || !source.trim()) return;
  const reason = window.prompt('请输入批准原因');
  if (!reason || !reason.trim()) return;
  try {
    await jsonFetch('api/v1/crm/base-data/name-mappings/approve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        alias: row.alias,
        expected: {
          alias: row.alias,
          canonical: row.canonical || '',
          note: row.note || '',
          source: row.source || '',
          review_status: row.review_status || 'pending',
        },
        source: source.trim(),
        reason: reason.trim(),
      }),
    });
    toast('名称映射已批准并记录审计');
    await loadCrmNameMappings();
  } catch (error) {
    toast(`${error.message || '映射审批失败'}。请刷新列表核对服务端状态。`, 'error');
  }
}

async function deleteCrmNameMapping(index) {
  const row = crmNameMappingRows[index];
  if (!row) return;
  if (!window.confirm(`确定删除名称映射“${row.alias}”→“${row.canonical}”？`)) return;
  const reason = window.prompt('请输入删除原因');
  if (!reason || !reason.trim()) return;
  const source = window.prompt('请输入来源或变更记录');
  if (!source || !source.trim()) { toast('请填写删除来源', 'error'); return; }
  try {
    await jsonFetch('api/v1/crm/base-data/name_mappings', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...row, source: source.trim(), reason: reason.trim() }),
    });
    toast('名称映射已删除并记录审计');
    await loadCrmNameMappings();
  } catch (error) {
    toast(`${error.message || '映射删除失败'}。请刷新列表核对服务端状态。`, 'error');
  }
}

function parseCrmScheduleCandidates() {
  const result = window.ScheduleCandidate.parse($('crmScheduleSourceText').value);
  crmScheduleCandidates = result.candidates;
  const status = $('crmScheduleParseStatus');
  const container = $('crmScheduleCandidates');
  if (result.errors.length) {
    status.textContent = '未生成候选';
    container.innerHTML = `${scheduleCandidateStaleNotice()}${result.errors.map((message) => `<div class="banner warn">${escapeHtml(message)}</div>`).join('')}`;
    return;
  }
  if (!crmScheduleCandidates.length) {
    status.textContent = '未找到候选';
    container.innerHTML = `${scheduleCandidateStaleNotice()}<p class="muted">输入字段文本后点击“解析候选”。</p>`;
    return;
  }
  const validCount = crmScheduleCandidates.filter((candidate) => candidate.adoptable).length;
  status.textContent = `候选 ${crmScheduleCandidates.length} 条 · 可采用 ${validCount} 条`;
  container.innerHTML = `${scheduleCandidateStaleNotice()}${crmScheduleCandidates.map((candidate, index) => {
    const fields = candidate.fields;
    const errors = candidate.errors.length
      ? `<div class="banner warn">${candidate.errors.map(escapeHtml).join('<br>')}</div>`
      : '<div class="banner info">字段格式可用；状态语义仍待客户确认。请人工核对日期、项目和原文。</div>';
    return `<article class="card card-pad mt-16">
      <div class="card-head"><div><h3>候选 ${index + 1} · 第 ${candidate.line_start}-${candidate.line_end} 行</h3><p>${escapeHtml(fields.project_name || '项目名称缺失')}</p></div>
        <button type="button" class="btn btn-secondary" data-crm-schedule-adopt="${index}" ${candidate.adoptable ? '' : 'disabled'}>人工确认并填入表单</button></div>
      ${errors}
      <div class="config-facts"><span>项目编号：${escapeHtml(fields.project_id || '未提供')}</span><span>计划日期：${escapeHtml(fields.start_date || '未提供')} – ${escapeHtml(fields.end_date || '未提供')}</span><span>状态原文：${escapeHtml(fields.status || '未提供')}</span><span>备注：${escapeHtml(fields.note || '未提供')}</span></div>
      <details><summary>查看来源原文（仅当前页面）</summary><pre>${escapeHtml(candidate.raw)}</pre></details>
    </article>`;
  }).join('')}`;
}

function scheduleCandidateStaleNotice() {
  if (!crmScheduleDraftGuard.isStale()) return '';
  return '<div class="banner warn">来源文本已变化，之前采纳的草稿已锁定。重新解析并采纳新候选，或明确解除候选关联后按人工录入继续。<br><button type="button" class="btn btn-secondary mt-16" data-crm-schedule-manualize>解除候选关联并转人工录入</button></div>';
}

function adoptCrmScheduleCandidate(index) {
  const candidate = crmScheduleCandidates[index];
  if (!candidate || !candidate.adoptable) return;
  const draft = window.ScheduleCandidate.toDraft(candidate);
  $('crmScheduleProjectId').value = draft.project_id;
  $('crmScheduleProjectName').value = draft.project_name;
  $('crmScheduleStart').value = draft.start_date;
  $('crmScheduleEnd').value = draft.end_date;
  $('crmScheduleStatus').value = draft.status;
  $('crmScheduleNote').value = draft.note;
  $('crmScheduleSource').value = draft.source;
  $('crmScheduleReason').value = '';
  crmScheduleDraftGuard.adopt();
  $('crmScheduleForm').scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('crmScheduleProjectName').focus({ preventScroll: true });
  toast('候选已填入草稿。完整原文已放入备注；请复核字段并手动填写变更原因后保存。');
}

function manualizeCrmScheduleDraft() {
  if (!crmScheduleDraftGuard.isStale()) return;
  if (!window.confirm('解除后保留当前业务字段，但会清空候选来源和原文备注。请之后填写人工来源和变更原因。继续吗？')) return;
  crmScheduleDraftGuard.manualize();
  $('crmScheduleSource').value = '';
  $('crmScheduleNote').value = '';
  $('crmScheduleReason').value = '';
  $('crmScheduleParseStatus').textContent = '已解除旧候选关联；请填写人工来源和变更原因。';
  $('crmScheduleCandidates').innerHTML = '<div class="banner info">候选关联已解除。当前表单保留业务字段；填写人工来源和变更原因后，可作为人工录入提交。</div>';
}

async function saveCrmSchedule(event) {
  event.preventDefault();
  if (crmScheduleDraftGuard.isStale()) {
    toast('候选来源已变化。请重新采纳候选，或先明确解除候选关联。', 'error');
    return;
  }
  const button = $('crmScheduleSave');
  const payload = {
    project_id: $('crmScheduleProjectId').value.trim(),
    project_name: $('crmScheduleProjectName').value.trim(),
    start_date: $('crmScheduleStart').value,
    end_date: $('crmScheduleEnd').value,
    status: $('crmScheduleStatus').value.trim(),
    note: $('crmScheduleNote').value.trim(),
    source: $('crmScheduleSource').value.trim(),
    reason: $('crmScheduleReason').value.trim(),
  };
  if (!payload.project_name || !payload.source || !payload.reason) {
    toast('请填写项目名称、来源和变更原因', 'error');
    return;
  }
  busy(button, true, '保存中…');
  try {
    let result;
    try {
      result = await jsonFetch('api/v1/crm/base-data/schedules', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    } catch (error) {
      if (error.statusCode !== 409 || !window.confirm('同一项目和日期已有内容不同的排期。确认用当前表单覆盖该条记录吗？')) throw error;
      result = await jsonFetch('api/v1/crm/base-data/schedules', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, force_replace: true }),
      });
    }
    $('crmScheduleForm').reset();
    crmScheduleDraftGuard.reset();
    toast(result.action === 'updated' ? '排期已更新并记录审计' : '排期已新增并记录审计');
    await loadCrmSchedules();
  } catch (error) {
    toast(`${error.message || '排期保存失败'}。请刷新列表核对服务端状态后再操作。`, 'error');
  } finally {
    busy(button, false);
  }
}

async function deleteCrmSchedule(index) {
  const row = crmScheduleRows[index];
  if (!row) return;
  if (!window.confirm(`确定删除“${row.project_name}” ${row.start_date || '未提供开始日期'} – ${row.end_date || '未提供结束日期'}？`)) return;
  const reason = window.prompt('请输入删除原因');
  if (!reason || !reason.trim()) return;
  const source = window.prompt('请输入来源或变更记录');
  if (!source || !source.trim()) {
    toast('请填写删除来源', 'error');
    return;
  }
  try {
    await jsonFetch('api/v1/crm/base-data/schedules', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...row, source: source.trim(), reason: reason.trim() }),
    });
    toast('排期已删除并记录审计');
    await loadCrmSchedules();
  } catch (error) {
    toast(`${error.message || '排期删除失败'}。请刷新列表核对服务端状态。`, 'error');
  }
}

function switchAdminView(view, section = '') {
  const canManageUsers = can('user:manage');
  const canManageCrmBase = can('crm:base:manage');
  const canWriteCrmPrices = can('crm:prices:manage') || canManageCrmBase;
  const canManageSchedules = can('schedule:manage') || canManageCrmBase;
  const crmView = ['crm-schedules', 'crm-qualifications', 'crm-prices', 'crm-name-mappings'].includes(view);
  const canOpenCrmView = view === 'crm-prices' ? canWriteCrmPrices
    : view === 'crm-schedules' ? canManageSchedules : canManageCrmBase;
  const servicePermissions = {
    registrations: 'customer-service:registration:review',
    orders: 'customer-service:order:review',
    'quote-inquiries': 'customer-service:quote:manage',
    conversations: 'customer-service:conversation:manage',
  };
  const canOpenServiceView = Boolean(servicePermissions[view] && can(servicePermissions[view]));
  if (!canManageUsers && view !== 'config' && !(crmView && canOpenCrmView) && !canOpenServiceView) view = 'config';
  state.adminView = view;
  $$('#adminTabs button').forEach((button) => {
    const tab = button.dataset.adminView;
    const visible = canManageUsers || (tab === 'config' && can('config:read'))
      || (tab === 'crm-schedules' && canManageSchedules)
      || (tab === 'crm-prices' && canWriteCrmPrices)
      || (['crm-qualifications', 'crm-name-mappings'].includes(tab) && canManageCrmBase)
      || (servicePermissions[tab] && can(servicePermissions[tab]));
    button.classList.toggle('hidden', !visible);
    if (button.dataset.adminView === 'crm-schedules') button.classList.toggle('hidden', !canManageSchedules);
    if (['crm-qualifications', 'crm-name-mappings'].includes(button.dataset.adminView)) button.classList.toggle('hidden', !canManageCrmBase);
    if (button.dataset.adminView === 'crm-prices') button.classList.toggle('hidden', !canWriteCrmPrices);
    button.classList.toggle('active', button.dataset.adminView === view);
  });
  $('adminUsersView').classList.toggle('hidden', view !== 'users');
  $('adminCrmSchedulesView').classList.toggle('hidden', view !== 'crm-schedules');
  $('adminCrmQualificationsView').classList.toggle('hidden', view !== 'crm-qualifications');
  $('adminCrmPricesView').classList.toggle('hidden', view !== 'crm-prices');
  $('adminCrmNameMappingsView').classList.toggle('hidden', view !== 'crm-name-mappings');
  $('adminRegistrationsView').classList.toggle('hidden', view !== 'registrations');
  $('adminOrdersView').classList.toggle('hidden', view !== 'orders');
  $('adminQuoteInquiriesView').classList.toggle('hidden', view !== 'quote-inquiries');
  $('adminConversationsView').classList.toggle('hidden', view !== 'conversations');
  $('adminAuditView').classList.toggle('hidden', view !== 'audit');
  $('adminConfigView').classList.toggle('hidden', view !== 'config');
  if (view === 'crm-schedules' && canManageSchedules) {
    $('crmScheduleForm').classList.toggle('hidden', !can('schedule:manage') && !canManageCrmBase);
    loadCrmSchedules();
  }
  if (view === 'crm-qualifications' && canManageCrmBase) loadCrmQualifications();
  if (view === 'crm-prices' && canWriteCrmPrices) {
    $('crmPriceForm').classList.toggle('hidden', !canWriteCrmPrices);
    loadCrmPrices();
  }
  if (view === 'crm-name-mappings' && canManageCrmBase) loadCrmNameMappings();
  if (view === 'registrations') loadCustomerRegistrations();
  if (view === 'orders') loadCustomerOrders();
  if (view === 'quote-inquiries') loadCustomerQuoteInquiries();
  if (view === 'conversations') loadCustomerConversations();
  if (view === 'audit') loadAudit(true);
  if (view === 'config') adminConfigLoad = loadAdminConfig().catch(showError);
  syncAdminLocation(view, view === 'config' ? section : '');
}

function defaultAdminView() {
  if (can('user:manage')) return 'users';
  for (const [view, permission] of [
    ['registrations', 'customer-service:registration:review'],
    ['orders', 'customer-service:order:review'],
    ['quote-inquiries', 'customer-service:quote:manage'],
    ['conversations', 'customer-service:conversation:manage'],
    ['crm-schedules', 'schedule:manage'],
    ['crm-prices', 'crm:prices:manage'],
  ]) {
    if (can(permission)) return view;
  }
  return 'config';
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
  $('quoteDemoPreviewBtn').addEventListener('click', async () => {
    const button = $('quoteDemoPreviewBtn');
    busy(button, true, '加载中…');
    $('quoteDemoPreviewNotice').classList.add('hidden');
    try {
      const data = await jsonFetch('api/v1/customer-service/demo/quote-preview');
      renderCustomerServicePreview(data);
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
    }
  });
  $('quoteFile').addEventListener('change', () => resetQuoteParseReview('文件已变化，请重新解析并复核。'));
  $('quoteDemoProject').addEventListener('input', () => {
    if (parsedQuoteReview && parsedQuoteReview.projectName !== $('quoteDemoProject').value.trim()) {
      resetQuoteParseReview('项目已变化，请重新解析并复核。');
    }
  });
  $('quoteParseReviewBtn').addEventListener('click', async () => {
    const button = $('quoteParseReviewBtn');
    busy(button, true, '解析中…');
    try {
      await beginQuoteParseReview();
      toast(`已解析 ${parsedQuoteReview.lines.length} 行；请核对来源行与字段后确认`);
    } catch (error) { showError(error); }
    finally { busy(button, false); }
  });
  $('quoteParseReview').addEventListener('input', (event) => {
    if (!event.target.matches('[data-parse-index][data-parse-field]') || !parsedQuoteReview) return;
    parsedQuoteReview.editRevision += 1;
    parsedQuoteReview.reviewedPayload = null;
    const confirmButton = $('quoteParseReviewConfirmBtn');
    if (confirmButton) confirmButton.disabled = false;
    const status = $('quoteParseReviewStatus');
    if (status) status.textContent = '复核表已修改，需再次提交并留痕后才能进行来源预览。';
  });
  $('quoteParseReview').addEventListener('click', async (event) => {
    const button = event.target.closest('#quoteParseReviewConfirmBtn');
    if (!button) return;
    busy(button, true, '记录复核…');
    try {
      const reviewed = await saveQuoteParseReview();
      toast(`解析复核已记录：${reviewed.review_id}`);
    } catch (error) {
      const status = $('quoteParseReviewStatus');
      if (status) status.textContent = error.message;
      showError(error);
    } finally {
      busy(button, false);
      if (parsedQuoteReview && parsedQuoteReview.reviewedPayload) button.disabled = true;
    }
  });
  $('quoteParsedPreviewBtn').addEventListener('click', async () => {
    const button = $('quoteParsedPreviewBtn');
    const projectName = $('quoteDemoProject').value.trim();
    if (!projectName) { toast('请填写检测项目名称', 'info'); return; }
    busy(button, true, '解析并匹配中…');
    $('quoteDemoPreviewNotice').classList.add('hidden');
    try {
      const payload = await parseSelectedQuoteLines(projectName);
      const data = await jsonFetch('api/v1/customer-service/demo/quote-preview/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderCustomerServicePreview(data);
      toast(`已解析 ${payload.lines.length} 行并完成合成目录比对；不生成报价`);
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
    }
  });
  $('quoteCrmPreviewBtn').addEventListener('click', async () => {
    const button = $('quoteCrmPreviewBtn');
    const projectName = $('quoteDemoProject').value.trim();
    if (!projectName) { toast('请填写检测项目名称', 'info'); return; }
    busy(button, true, '解析并匹配 CRM 中…');
    $('quoteDemoPreviewNotice').classList.add('hidden');
    try {
      const payload = await parseSelectedQuoteLines(projectName);
      const data = await jsonFetch('api/v1/customer-service/quote-preview/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      renderCustomerServicePreview(data);
      toast(`已解析 ${payload.lines.length} 行并匹配当前 CRM 只读数据；不生成报价`);
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
    }
  });
  $('quoteCrmExportBtn').addEventListener('click', async () => {
    const button = $('quoteCrmExportBtn');
    const projectName = $('quoteDemoProject').value.trim();
    try {
      const payload = await parseSelectedQuoteLines(projectName);
      busy(button, true, '重新匹配并生成中…');
      const response = await fetch('api/v1/customer-service/quote-preview/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        let detail = `请求失败（${response.status}）`;
        try { detail = (await response.json()).detail || detail; } catch (_) { /* Keep the status message. */ }
        throw new Error(detail);
      }
      const type = response.headers.get('content-type') || '';
      if (!type.includes('spreadsheetml.sheet')) throw new Error('服务端返回的文件类型无效，已拒绝下载');
      const blob = await response.blob();
      if (!blob.size) throw new Error('服务端返回空文件，已拒绝下载');
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'CRM内部复核草稿.xlsx';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast('已下载内部 CRM 复核草稿；未生成正式报价或写回 CRM');
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
    }
  });
  $('quoteDemoPreview').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-cs-price-review]');
    if (!button) return;
    const preview = customerServicePreviewData;
    const line = preview && preview.lines && preview.lines[Number(button.dataset.lineIndex)];
    if (!line || preview.environment !== 'internal' || !preview.source_mode || !preview.source_revision) {
      toast('当前预览状态已失效，请重新加载 CRM 预览', 'error');
      return;
    }
    const request = line.request || {};
    const evidence = line.input_evidence || {};
    const payload = {
      project_name: request.project_name || '',
      source_mode: preview.source_mode,
      source_revision: preview.source_revision,
      line_id: line.line_id || '',
      selected_record_id: button.dataset.candidateId || '',
      item_name: request.item_name || '',
      standard: request.standard || '',
      sample_name: request.sample_name || '',
      quantity: request.quantity ?? null,
      unit: request.unit || '',
      source_row: evidence.source_row ?? null,
      confidence: evidence.parse_confidence ?? null,
    };
    let reviewRecorded = false;
    busy(button, true, '记录中…');
    try {
      const result = await jsonFetch('api/v1/customer-service/quote-preview/price-reviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      reviewRecorded = true;
      toast(`审核意见已记录（${result.review_id}）；未确认报价，也未写回 CRM`);
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
      if (reviewRecorded) {
        button.disabled = true;
        button.textContent = '已记录内部审核意见';
      }
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
  $('inquirySaveDraft').addEventListener('click', async () => {
    try {
      const inquiry = await saveQuoteInquiryDraft();
      $('inquiryStatus').textContent = `草稿已保存：${inquiry.inquiry_id}`;
      toast('内部询价草稿已保存');
    } catch (error) {
      $('inquiryStatus').textContent = error.message;
      toast(error.message, 'error');
    }
  });
  $('inquirySubmit').addEventListener('click', submitQuoteInquiry);
  $('inquiryRefresh').addEventListener('click', loadMyQuoteInquiries);
  $('inquiryNew').addEventListener('click', async () => {
    const actor = String(state.me?.username || '');
    const pendingKey = actor ? `p0-quote-inquiry-create:${actor}` : '';
    if (pendingKey) {
      let pending = null;
      try { pending = JSON.parse(sessionStorage.getItem(pendingKey) || 'null'); }
      catch (error) { toast('询价重试记录不可读取，已保留当前资料', 'error'); return; }
      if (pending && pending.confirmedInquiryId) {
        try { sessionStorage.removeItem(pendingKey); }
        catch (error) { toast('旧询价已确认，但本机重试记录无法清理；请稍后重试新建', 'error'); return; }
      } else if (pending) {
        try {
          state.quoteInquiryId = '';
          restoreQuoteInquiryPayload(pending.payload);
          const confirmed = await saveQuoteInquiryDraft();
          if (!confirmed || confirmed.inquiry_id !== state.quoteInquiryId) {
            toast('旧询价未能确认，原资料已保留', 'error');
            return;
          }
        } catch (error) {
          $('inquiryStatus').textContent = error.message;
          toast('旧询价未能确认，原资料已保留', 'error');
          return;
        }
      }
    }
    state.quoteInquiryId = '';
    ['inquiryProject', 'inquiryItem', 'inquirySample', 'inquiryStandard', 'inquiryQuantity', 'inquiryUnit', 'inquiryContext'].forEach((id) => { $(id).value = ''; });
    $('inquiryStatus').textContent = '新建草稿';
    $('inquiryPreview').classList.add('hidden');
  });
  $('myQuoteInquiries').addEventListener('click', (event) => {
    const button = event.target.closest('[data-my-inquiry]');
    if (button) openMyQuoteInquiry(button.dataset.myInquiry);
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
  $('crmScheduleRefresh').addEventListener('click', loadCrmSchedules);
  $('crmScheduleParse').addEventListener('click', parseCrmScheduleCandidates);
  $('crmScheduleSourceText').addEventListener('input', () => {
    crmScheduleCandidates = [];
    crmScheduleDraftGuard.sourceChanged();
    $('crmScheduleCandidates').innerHTML = `${scheduleCandidateStaleNotice()}<p class="muted">文本已变化，请重新解析。</p>`;
    $('crmScheduleParseStatus').textContent = crmScheduleDraftGuard.isStale() ? '旧草稿已锁定' : '';
  });
  $('crmScheduleCandidates').addEventListener('click', (event) => {
    const button = event.target.closest('[data-crm-schedule-adopt]');
    if (button) { adoptCrmScheduleCandidate(Number(button.dataset.crmScheduleAdopt)); return; }
    if (event.target.closest('[data-crm-schedule-manualize]')) manualizeCrmScheduleDraft();
  });
  $('crmScheduleForm').addEventListener('submit', saveCrmSchedule);
  window.ScheduleCandidate.bindFormReset($('crmScheduleForm'), crmScheduleDraftGuard, () => {
    crmScheduleCandidates = [];
    $('crmScheduleParseStatus').textContent = '';
    $('crmScheduleCandidates').innerHTML = '<p class="muted">表单已清空。需要候选时请重新解析。</p>';
  });
  $('crmScheduleBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-crm-schedule-delete]');
    if (button) deleteCrmSchedule(Number(button.dataset.crmScheduleDelete));
  });
  $('crmQualificationRefresh').addEventListener('click', loadCrmQualifications);
  $('crmQualificationForm').addEventListener('submit', saveCrmQualification);
  $('crmQualificationBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-crm-qualification-delete]');
    if (button) deleteCrmQualification(Number(button.dataset.crmQualificationDelete));
  });
  $('crmPriceRefresh').addEventListener('click', loadCrmPrices);
  $('crmPriceForm').addEventListener('submit', saveCrmPrice);
  $('crmPriceBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-crm-price-delete]');
    if (button) deleteCrmPrice(Number(button.dataset.crmPriceDelete));
  });
  $('crmNameMappingRefresh').addEventListener('click', loadCrmNameMappings);
  $('crmNameMappingForm').addEventListener('submit', saveCrmNameMapping);
  $('crmNameMappingBody').addEventListener('click', (event) => {
    const approve = event.target.closest('[data-crm-name-mapping-approve]');
    if (approve) { approveCrmNameMapping(Number(approve.dataset.crmNameMappingApprove)); return; }
    const remove = event.target.closest('[data-crm-name-mapping-delete]');
    if (remove) deleteCrmNameMapping(Number(remove.dataset.crmNameMappingDelete));
  });
  $('registrationRefresh').addEventListener('click', loadCustomerRegistrations);
  $('registrationStatusFilter').addEventListener('change', loadCustomerRegistrations);
  $('registrationBody').addEventListener('click', (event) => {
    const retry = event.target.closest('[data-registration-review-retry]');
    if (retry) { decideCustomerRegistration(retry.dataset.registrationReviewRetry, ''); return; }
    const button = event.target.closest('[data-registration-decision]');
    if (!button) return;
    decideCustomerRegistration(
      button.dataset.registrationId,
      button.dataset.registrationDecision,
      button.dataset.registrationRuleRevision || null,
      Number(button.dataset.registrationRevision) || null,
    );
  });
  $('orderRefresh').addEventListener('click', loadCustomerOrders);
  $('orderStatusFilter').addEventListener('change', loadCustomerOrders);
  $('orderBody').addEventListener('click', (event) => {
    const retry = event.target.closest('[data-order-review-retry]');
    if (retry) { decideCustomerOrder(retry.dataset.orderReviewRetry, ''); return; }
    const button = event.target.closest('[data-order-decision]');
    if (button) decideCustomerOrder(button.dataset.orderId, button.dataset.orderDecision, Number(button.dataset.orderRevision));
  });
  $('quoteInquiryRefresh').addEventListener('click', loadCustomerQuoteInquiries);
  $('quoteInquiryStatusFilter').addEventListener('change', loadCustomerQuoteInquiries);
  $('quoteInquiryBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-inquiry-action]');
    if (button) handleCustomerQuoteInquiryAction(button);
  });
  $('conversationRefresh').addEventListener('click', loadCustomerConversations);
  $('conversationStatusFilter').addEventListener('change', loadCustomerConversations);
  $('conversationCompanyFilter').addEventListener('input', loadCustomerConversations);
  $('conversationPriorityFilter').addEventListener('change', loadCustomerConversations);
  $('conversationUnreadFilter').addEventListener('change', loadCustomerConversations);
  $('conversationBody').addEventListener('click', (event) => {
    const button = event.target.closest('[data-conversation-action]');
    if (button) handleCustomerConversationAction(button).catch((error) => toast(error.message, 'error'));
  });
  $('reportConfigSave').addEventListener('click', () => saveConfigSection('report', $('reportConfigSave'), collectReportConfig));
  $('standardConfigSave').addEventListener('click', () => saveConfigSection('standard', $('standardConfigSave'), collectStandardConfig));
  $('quoteConfigSave').addEventListener('click', () => saveConfigSection('quote', $('quoteConfigSave'), collectQuoteConfig));
  $('businessConfigSave').addEventListener('click', () => saveConfigSection('business', $('businessConfigSave'), collectBusinessConfig));
  $('cfgBizBlacklistImport').addEventListener('click', importBusinessBlacklist);
  $('cfgBizBlacklistExport').addEventListener('click', () => {
    window.location = 'api/v1/admin/config/business/blacklist.csv';
  });
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
    if (button.dataset.act === 'oidc') {
      const issuers = (button.dataset.issuers || '').split('|').filter(Boolean);
      const currentIssuer = issuers[0] || '';
      const issuer = window.prompt('OIDC 身份提供方 issuer 地址', currentIssuer);
      if (issuer === null || !issuer.trim()) return;
      if (issuers.includes(issuer.trim().replace(/\/$/, ''))) {
        const remove = await confirmDialog({
          title: '解除 SSO 绑定？',
          message: `确认解除 ${username} 在该身份提供方的唯一身份绑定吗？解除后该账号不能通过该提供方登录。`,
          confirmText: '解除绑定',
          tone: 'danger',
        });
        if (remove) {
          await jsonFetch(`api/v1/auth/users/${encodeURIComponent(username)}/oidc-identity`, {
            method: 'DELETE', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issuer: issuer.trim() }),
          });
          toast(`已解除 ${username} 的 SSO 绑定`);
          await loadAdmin();
        }
        return;
      }
      const subject = window.prompt('输入 IdP 已核验的稳定 subject（不能用邮箱代替）');
      if (subject === null || !subject.trim()) return;
      try {
        await jsonFetch(`api/v1/auth/users/${encodeURIComponent(username)}/oidc-identity`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ issuer: issuer.trim(), subject: subject.trim() }),
        });
        toast(`已更新 ${username} 的 SSO 身份绑定`);
        await loadAdmin();
      } catch (error) { showError(error); }
    } else if (button.dataset.act === 'review-permissions') {
      const choices = ['report:read', 'report:audit', 'standard:review'];
      const current = button.dataset.permissions || '';
      const selected = window.prompt(`逐账号设置审核权限，输入逗号分隔的权限代码；留空撤销。\n可选：${choices.join(', ')}`, current);
      if (selected === null) return;
      const permissions = selected.split(/[,，\s]+/).filter(Boolean);
      if (permissions.some((permission) => !choices.includes(permission))) {
        toast('仅可设置列出的报告审核/标准复核权限', 'error');
        return;
      }
      const reason = window.prompt('填写逐账号确认依据（必填）');
      if (reason === null) return;
      try {
        await jsonFetch(`api/v1/auth/users/${encodeURIComponent(username)}/review-permissions`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ permissions, reason }),
        });
        toast(`已更新 ${username} 的审核权限`);
        await loadAdmin();
      } catch (error) { showError(error); }
    } else if (button.dataset.act === 'projects') {
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
    $('nuRole').value = 'business_manager';
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
  const initialAdminView = ['users', 'crm-schedules', 'crm-qualifications', 'crm-prices', 'crm-name-mappings', 'audit', 'config'].includes(wantedView) ? wantedView : 'users';
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
