import * as ui from './ui-kit.bundle.js';

/* Standalone design simulation: deliberately no API, persistence, provider or task runtime. */
const $ = (id) => document.getElementById(id);
const node = (tag, className = '', text = '') => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
};
const append = (parent, ...children) => { parent.append(...children); return parent; };
const button = (label, onClick, variant = 'ghost', id = '', options = {}) => {
  const el = ui.createButton({ label, onClick, variant, size: 'sm', wrap: true, ...options });
  if (id) el.id = id;
  return el;
};
function orbit() {
  const box = node('span');
  box.innerHTML = '<svg width="26" height="26" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><ellipse cx="16" cy="16" rx="13" ry="7" transform="rotate(-38 16 16)"/><ellipse cx="16" cy="16" rx="13" ry="7" transform="rotate(38 16 16)"/><path d="M16 10l1.6 4.4L22 16l-4.4 1.6L16 22l-1.6-4.4L10 16l4.4-1.6z" fill="currentColor" stroke="none"/><circle cx="27" cy="10" r="2" fill="currentColor" stroke="none"/></svg>';
  return box.firstElementChild;
}
function icon(label, onClick, svg) {
  const span = node('span');
  span.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${svg}</svg>`;
  return ui.createIconButton({ label, icon: span.firstElementChild, size: 'sm', onClick });
}
const stories = [
  { id: 'research', title: '登录方案研究', short: '方案研究', letter: '研', detail: '两种方案的取舍与推荐', type: 'research' },
  { id: 'frontend', title: '登录前端实现', short: '前端实现', letter: '设', detail: '会话状态与登录入口', type: 'frontend' },
  { id: 'compat', title: '接口兼容检查', short: '兼容检查', letter: '验', detail: '依赖前端文件实际交付', type: 'compat' },
  { id: 'notes', title: '一些产品灵感', short: '产品灵感', letter: '✎', detail: '普通对话 · 不参与此任务', type: 'notes' },
];
let state = { view: 'coordinator', researchReviewed: false, grant: 'none', result: false,
  fileReviewed: false, delivery: 'held', fixture: 'normal', bound: true };
const drafts = new Map();
const messages = new Map();
let query = '';
let modalOpen = false;
let mobileOpener = null;
const sha = '91a7df16f550135b64a9f1f2c3d4e5a6b7c8d9e0';
const themes = ['tofu', 'dark', 'light'];
const params = new URLSearchParams(location.search);
document.documentElement.dataset.theme = themes.includes(params.get('theme')) ? params.get('theme') : 'tofu';

function badge(label, tone = '') {
  return append(node('span', `entry-status ${tone ? 'status-' + tone : ''}`), node('span', 'status-dot'), node('span', '', label));
}
function fact(label, value) {
  return append(node('div', 'receipt'), node('span', 'scope-label', label), node('strong', '', value));
}
function status(id) {
  if (id === 'research') return state.researchReviewed ? ['已审阅', 'success'] : ['有新结果', 'success'];
  if (id === 'frontend') {
    if (state.grant === 'unknown') return ['授权回执未知', 'warning'];
    if (state.grant === 'withdrawn') return ['授权已撤销 · 取消待确认', 'warning'];
    if (state.result) return state.fileReviewed ? ['已审阅 · 交付' + (state.delivery === 'delivered' ? '已确认' : '未确认'), state.delivery === 'delivered' ? 'success' : 'warning'] : ['执行完成 · 待审阅', 'success'];
    if (state.grant === 'active') return ['进行中', 'info'];
    return [state.researchReviewed ? '等待授权' : '等待方案审阅', ''];
  }
  if (id === 'compat') return [state.delivery === 'delivered' ? '依赖已满足 · 未授权' : '等待文件交付', ''];
  return ['普通对话', ''];
}
function nextAction() {
  if (!state.bound) return ['设置 Coordinator', bindCoordinator];
  if (state.grant === 'unknown') return ['核对原请求', reconcileGrant];
  if (state.grant === 'withdrawn') return ['查看撤销回执', showEvidence];
  if (!state.researchReviewed) return ['审阅方案', () => review('research')];
  if (state.grant === 'none') return ['确认执行范围', authorize];
  if (!state.result) return ['打开前端对话', () => select('frontend')];
  if (!state.fileReviewed) return ['审阅实现结果', () => review('frontend')];
  if (state.delivery === 'blocked') return ['查看门禁原因', showEvidence];
  if (state.delivery !== 'delivered') return [state.delivery === 'unknown' ? '核对交付回执' : '确认交付', publish];
  return ['查看交付证据', showEvidence];
}
function attention() {
  if (!state.bound) return '协调角色未设置';
  if (state.grant === 'withdrawn') return '前端授权已撤销，取消待确认';
  if (state.grant === 'unknown') return '前端授权回执未知，暂不重发';
  if (!state.researchReviewed) return '登录改造：等待你审阅方案';
  if (state.grant === 'none') return '方案已接受，等待有限授权';
  if (!state.result) return '前端实现进行中，兼容检查等待上游';
  if (!state.fileReviewed) return '前端结果已到，等待你审阅';
  if (state.delivery === 'delivered') return '文件交付已确认，后续检查尚未授权';
  if (state.delivery === 'blocked') return '交付受阻：缺少项目验证配置';
  return state.delivery === 'unknown' ? '交付回执未知，不重复发布' : '实现已接受，文件尚未交付';
}
function announce(text) { $('announcements').textContent = text; }
function toast(text) { ui.toast(text, { variant: 'info' }); announce(text); }
function canFocus(el) {
  return Boolean(el && el !== document.body && el !== document.documentElement
    && el.isConnected && el.getClientRects().length && !el.matches(':disabled')
    && !el.closest('[inert],[hidden]') && getComputedStyle(el).visibility === 'visible');
}
function restoreFocus(opener) {
  if (canFocus(opener)) { opener.focus(); return; }
  const fallback = [$('task-primary'), $('conversation-title'), mobileOpener]
    .find(canFocus);
  fallback?.focus();
}
async function drawer(title, content) {
  if (modalOpen) return;
  modalOpen = true;
  const opener = document.activeElement;
  const panel = ui.createDrawer({ title, body: content });
  panel.el.setAttribute('aria-label', title);
  document.body.append(panel.el);
  await panel.open();
  panel.el.remove();
  modalOpen = false;
  restoreFocus(opener);
}
async function dialog(title, body, actions, setup) {
  if (modalOpen) return null;
  modalOpen = true;
  const opener = document.activeElement;
  const panel = ui.createDialog({ title, body, actions });
  panel.el.setAttribute('aria-label', title);
  document.body.append(panel.el);
  setup?.(panel);
  const answer = await panel.open();
  panel.el.remove();
  modalOpen = false;
  restoreFocus(opener);
  return answer;
}
function wireThreadHighlight(el, id) {
  el.dataset.threadId = id;
  const paint = (on) => {
    for (const target of document.querySelectorAll('[data-thread-id]')) {
      if (target.dataset.threadId === id) target.dataset.linked = String(on);
    }
  };
  el.addEventListener('pointerenter', () => paint(true));
  el.addEventListener('pointerleave', () => paint(el.contains(document.activeElement)));
  el.addEventListener('focusin', () => paint(true));
  el.addEventListener('focusout', (event) => { if (!el.contains(event.relatedTarget)) paint(false); });
}
function sidebarEntry(id, title, description, marker, statusText, tone, pinned = false) {
  const row = node('div', 'conversation-entry' + (pinned ? ' coordinator-entry' : ''));
  row.dataset.active = String(state.view === id);

  wireThreadHighlight(row, id);
  const btn = button(title, () => select(id), 'ghost', `nav-${id}`);
  if (state.view === id) btn.setAttribute('aria-current', 'page');
  const entryIcon = node('span', 'entry-icon', pinned ? '' : marker);
  if (pinned) entryIcon.append(orbit());
  append(row, append(node('div', 'entry-title'), entryIcon, btn));
  row.append(node('div', 'entry-description', description));
  if (statusText) row.append(badge(statusText, tone));
  return row;
}
function renderSidebar() {
  $('coordinator-entry').replaceChildren(sidebarEntry('coordinator', 'Coordinator', attention(), '',
    !state.bound ? '未绑定' : state.grant === 'active' && !state.result ? '进行中' : state.delivery === 'delivered' ? '依赖已满足' : '需你确认',
    state.grant === 'active' && !state.result ? 'info' : 'warning', true));
  const list = $('conversation-list');
  list.replaceChildren();
  const filtered = stories.filter((story) => (story.title + story.detail).toLowerCase().includes(query.toLowerCase()));
  for (const story of filtered) {
    const [label, tone] = status(story.id);
    list.append(sidebarEntry(story.id, story.title, story.detail, story.letter, label, tone));
  }
  if (!filtered.length) list.append(node('div', 'search-empty', '示例列表没有匹配项。清空搜索可返回。'));
}
function byline(name = 'Coordinator', letter = '') {
  const mark = node('span', 'message-mark', letter);
  if (!letter) mark.append(orbit());
  return append(node('div', 'message-byline'), mark, node('span', '', name), node('time', '', '14:32'));
}
function human(text) { return append(node('div', 'human-message'), node('p', '', text)); }
function taskCanvas() {
  const card = node('section', 'task-canvas');
  card.setAttribute('aria-label', '登录改造任务');
  const head = append(node('div', 'task-canvas-head ui-row ui-row--between'),
    append(node('div', 'task-heading'), node('div', 'eyebrow', 'ONE GOAL · THREE THREADS'), node('h3', '', '登录体验改造'), node('div', 'task-subtitle', '工作分开推进，决定留在这里。')),
    button('详情 ↗', showEvidence, 'ghost', 'task-details', { wrap: false }));
  card.append(head);
  const constellation = node('div', 'constellation');
  const stages = [
    ['方案', state.researchReviewed ? 'done' : 'current', state.researchReviewed ? '✓' : '01'],
    ['实现', state.result ? 'done' : state.researchReviewed ? 'current' : '', state.result ? '✓' : '02'],
    ['交付', state.delivery === 'delivered' ? 'done' : state.fileReviewed ? 'current' : '', state.delivery === 'delivered' ? '✓' : '03'],
  ];
  for (const [label, css, number] of stages) constellation.append(append(node('div', 'constellation-stage ' + css), node('span', 'stage-orb', number), node('span', '', label)));
  card.append(constellation);
  const rows = node('div', 'task-rows');
  for (const story of stories.slice(0, 3)) {
    const [label, tone] = status(story.id);
    const detail = node('span', 'task-detail' + (tone ? ' status-' + tone : ''), label);
    const row = append(node('div', 'task-row'), node('span', 'worker-avatar ' + story.type, story.letter),
      append(node('div', 'task-title'), button(story.title + ' ↗', () => select(story.id), 'ghost', `task-${story.id}`)), detail);
    wireThreadHighlight(row, story.id);
    rows.append(row);
  }
  card.append(rows);
  const action = nextAction();
  const copy = append(node('div', 'task-action-copy'), node('span', 'action-marker', '✳'), node('strong', '', attention()));
  copy.append(node('small', '', state.delivery === 'delivered' ? '下一项工作仍需单独授权，不会自动启动。' : '审阅、执行与文件交付，是三个独立决定。'));
  card.append(append(node('div', 'task-action'), copy, button(action[0] + ' →', action[1], 'secondary', 'task-primary')));
  return card;
}
function coordinatorThread() {
  const assistant = node('article');
  assistant.append(byline());
  const prose = node('div', 'assistant-prose');
  append(prose, node('h2', '', '把复杂的事，聊清楚。'),
    node('p', '', '方案研究已经回来。工作在各自的对话里推进，需要你做的决定留在这里。'));
  assistant.append(prose, taskCanvas(), node('p', 'inline-footnote', '随时进入工作者对话看细节，再回到这里继续。无需切换工作台。'));
  $('thread').append(node('div', 'thread-date', '今天 · 产品研发'), human('我们把登录体验重新做一下。先研究方案，确定后再开始实现。'), assistant);
}
function researchPaper() {
  const paper = node('div', 'evidence-paper');
  append(paper, node('div', 'eyebrow', 'RESEARCH NOTE / 01'), node('h3', '', '少一次打断，多一点连续。'),
    node('p', '', '建议采用方案 B：保留当前会话，在需要身份验证时渐进提示。不把用户带去一个完全不同的页面。'));
  const options = node('div', 'option-grid');
  options.append(append(node('div', 'option-paper'), node('strong', '', 'A · 独立登录页'), node('p', '', '边界清晰，但用户需要离开当前任务。适合首次登录。')),
    append(node('div', 'option-paper recommended'), node('strong', 'status-success', 'B · 原地渐进登录 · 推荐'), node('p', '', '对话不中断，失败可恢复。已有用户优先采用这个路径。')));
  paper.append(options, node('p', '', '待确认：采用 B 作为主路径，保留 A 用于首次登录与故障恢复。'),
    append(node('div', 'evidence-meta'), node('span', '', '示例研究结果 · 版本 R1'), node('span', '', '执行完成 ≠ 已审阅')));
  return paper;
}
function frontendPaper() {
  const paper = node('div', 'evidence-paper');
  append(paper, node('div', 'eyebrow', 'IMPLEMENTATION / R1'), node('h3', '', state.result ? '实现结果，等你看一眼。' : '执行前，先把边界说清。'));
  if (state.result) append(paper,
    node('p', '', '示例结果包含原地登录入口、可恢复错误状态和会话连续性测试。当前文件仍在隔离工作区，审阅接受不会自动交付。'),
    node('div', 'file-path', 'src/auth/session-entry.ts'), node('div', 'file-path', 'src/auth/session-continuity.test.ts'),
    append(node('div', 'evidence-meta'), node('span', '', '示例 checkpoint'), node('span', 'code-pin', sha)));
  else append(paper, node('p', '', status('frontend')[0]), node('p', '', '仅允许读写 src/auth/。不允许 Shell、外部工具、子任务或后台续费。浏览和对话都不会启动执行。'));
  return paper;
}
function workerThread() {
  const story = stories.find((s) => s.id === state.view);
  const context = append(node('div', 'worker-context'), node('span', '', story.type === 'notes' ? '普通对话 · 未关联协作任务' : '登录体验改造 / ' + story.short), button('返回 Coordinator ↗', () => select('coordinator'), 'ghost', 'back-coordinator'));
  $('thread').append(context);
  const article = node('article');
  article.append(byline(story.title, story.letter));
  if (story.id === 'research') {
    article.append(researchPaper(), button(state.researchReviewed ? '查看审阅记录' : '审阅这个方案', () => review('research'), 'secondary', 'worker-review'));
  } else if (story.id === 'frontend') {
    article.append(frontendPaper());
    if (state.result && !state.fileReviewed) article.append(button('审阅实现结果', () => review('frontend'), 'secondary', 'worker-review'));
    else if (state.grant === 'none') article.append(button('返回协调者确认范围', () => select('coordinator'), 'secondary'));
    else if (state.grant === 'active' && !state.result) article.append(button('撤销本次授权', withdraw, 'danger', 'worker-withdraw'));
  } else if (story.id === 'compat') {
    const prose = node('div', 'assistant-prose');
    append(prose, node('h2', '', '等上游真正交付，再开始。'), node('p', '', '不会把“方案接受”或“实现完成”当成可用文件。此检查依赖前端结果 R1 的准确交付回执。'), node('p', '', status('compat')[0]));
    article.append(prose, button('查看依赖证据', showEvidence, 'secondary'));
  } else article.append(append(node('div', 'assistant-prose'), node('h2', '', '留一点空间，给新想法。'), node('p', '', '普通对话仍然是普通对话。置顶、命名和打开它，都不会赋予协调角色。')));
  $('thread').append(article);
}
function render() {
  const previousFocus = document.activeElement;
  const previousFocusId = previousFocus?.id;
  renderSidebar();
  const story = stories.find((s) => s.id === state.view);
  $('conversation-title').textContent = story ? story.title : 'Coordinator';
  $('conversation-title').tabIndex = -1;
  $('conversation-subtitle').textContent = story ? '产品研发 · ' + (story.type === 'notes' ? '普通对话' : '登录体验改造') : '一个目标，一个对话。 · 设计预览';
  $('header-symbol').replaceChildren(story ? node('span', '', story.letter) : orbit());
  $('header-actions').replaceChildren(button(window.matchMedia('(max-width:720px)').matches ? '脉络' : '任务脉络', showEvidence, 'ghost', 'header-evidence', { wrap: false }), icon('关于协调角色', roleInfo, '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>'));
  $('thread').replaceChildren();
  if (state.view === 'coordinator') coordinatorThread(); else workerThread();
  for (const entry of messages.get(state.view) || []) $('thread').append(entry.kind === 'human' ? human(entry.text) : append(node('article'), byline(state.view === 'coordinator' ? 'Coordinator' : '示例工作者'), node('p', 'inline-footnote', entry.text)));
  input.value = drafts.get(state.view) || '';
  $('composer-context').textContent = '✳ 发给 ' + (story ? story.title : 'Coordinator');
  $('suggestions').replaceChildren(button('接下来需要我做什么？', () => addMessage('接下来需要我做什么？'), 'ghost'), button('查看任务脉络 ↗', showEvidence, 'ghost'));
  $('shell').dataset.mobileList = 'false';
  $('main').inert = false;
  if (previousFocusId && !previousFocus.isConnected) {
    ($(previousFocusId) || $('conversation-title')).focus();
  }
}
function select(view) {
  drafts.set(state.view, input.value);
  state.view = view;
  render();
  $('thread-scroll').scrollTop = 0;
  announce('已打开' + $('conversation-title').textContent);
  $('conversation-title').focus();
}
function addMessage(text) {
  if (!text.trim()) { input.focus(); return; }
  if (text.length > 4000) { toast('原型输入最多 4000 字。'); return; }
  const list = messages.get(state.view) || [];
  if (list.length >= 20) { toast('原型保留最多 10 组本地消息，请重置演示。'); return; }
  list.push({ kind: 'human', text }, { kind: 'assistant', text: state.view === 'coordinator' ? '原型回复：' + attention() + '。这条消息只保存在本页，不会调用模型或授予执行权限。' : '原型回复：消息保留在这个示例对话中，未发给真实工作者，也不启动执行。' });
  messages.set(state.view, list);
  drafts.set(state.view, '');
  render();
  $('thread-scroll').scrollTop = $('thread-scroll').scrollHeight;
  input.focus();
}
async function review(id) {
  const research = id === 'research';
  const content = node('div', 'detail-stack');
  content.append(node('div', 'dialog-note', '示例审阅 · 接受仅记录审阅，不启动执行，也不交付文件。'), research ? researchPaper() : frontendPaper());
  if (research ? state.researchReviewed : state.fileReviewed) {
    content.append(fact('审阅记录', '已接受 · 结果 R1'), fact('文件交付', research ? '不适用' : state.delivery === 'delivered' ? '示例回执已确认' : '未确认'));
    return drawer('审阅记录', content);
  }
  if (!research && !state.result) return;
  const answer = await dialog(research ? '审阅登录方案 · R1' : '审阅实现结果 · R1', content, [
    { id: 'cancel', label: '稍后再看' }, { id: 'rework', label: '提出修改' }, { id: 'accept', label: research ? '接受方案 B' : '接受实现结果', variant: 'secondary' },
  ]);
  if (answer === 'accept') {
    if (research) state.researchReviewed = true; else state.fileReviewed = true;
    render();
    toast('示例审阅已接受；未启动新任务，未交付文件。');
    $('task-primary')?.focus();
  } else if (answer === 'rework') {
    input.value = research ? '请修改方案 R1：' : '请修改实现结果 R1：';
    drafts.set(state.view, input.value);
    input.focus();
    toast('已准备修改意见草稿。返工需要新的有限授权。');
  }
}
async function authorize() {
  if (!state.bound || !state.researchReviewed || state.grant !== 'none') return;
  const content = node('div', 'detail-stack');
  append(content, node('p', '', '你将允许「登录前端实现」按已接受方案 B 执行一次有限文件任务。这里演示确认流程，不会真正执行。'),
    append(node('div', 'receipt-grid'), fact('执行对象', '登录前端实现'), fact('模型', '示例模型 · 不调用'), fact('总 Token 上限', '12,000'), fact('请求 / 有效期', '最多 3 次 / 30 分钟')));
  const pathField = ui.createField({ label: '允许读写的相对路径', value: 'src/auth/', hint: '仅示例路径；修改范围会清除确认。', controlId: 'grant-path' });
  const choice = ui.createChoice({ label: '我已确认对象、范围及有限预算（仅演示）', onChange: () => {} });
  content.append(pathField.el, node('div', 'dialog-note', '允许：read_files / write_file / edit_file。禁止 Shell、外部工具、子任务。Token 上限不是费用保证；未知付费回执不得补发或恢复额度。'), choice.el);
  const answer = await dialog('确认本次执行范围', content, [{ id: 'cancel', label: '取消' }, { id: 'authorize', label: '确认授权 · 演示', variant: 'secondary' }], (panel) => {
    const action = panel.el.querySelector('.ui-dialog__footer button:last-child');
    action.disabled = true;
    choice.input.addEventListener('change', () => { action.disabled = !choice.input.checked || pathField.control.value !== 'src/auth/'; });
    pathField.control.addEventListener('input', () => { choice.input.checked = false; action.disabled = true; });
  });
  if (answer === 'authorize') {
    state.grant = state.fixture === 'uncertain' ? 'unknown' : 'active';
    render();
    toast(state.grant === 'unknown' ? '示例授权回执未知：保留原请求，不创建第二次授权。' : '模拟有限任务已开始。没有真实执行或费用。');
    $('task-primary')?.focus();
  }
}
async function reconcileGrant() {
  const content = node('div', 'detail-stack');
  append(content, node('p', '', '授权请求 demo-command-01 已提交，但没有得到确定回执。保持原请求身份，查询不会再次授权或补充预算。'), fact('执行', '未知'), fact('授权', '原请求 · 不新建'), button('模拟收到原请求回执', () => {
    state.grant = 'active';
    document.querySelector('dialog[open]')?.close('');
    render();
    toast('已模拟核对同一请求，未重复执行。');
  }, 'secondary'));
  await drawer('授权回执未知', content);
}
async function publish() {
  if (!state.result || !state.fileReviewed || state.grant !== 'active') return;
  if (state.delivery === 'unknown') {
    const content = node('div', 'detail-stack');
    content.append(node('p', '', '发布请求已提出，交付回执仍未知。不会把“请求成功”当成文件已到达。'), node('div', 'code-pin', sha),
      button('模拟准确交付回执到达', () => {
        state.delivery = 'delivered';
        document.querySelector('dialog[open]')?.close('');
        render(); toast('示例交付回执已确认。后续检查仍未获得执行授权。');
      }, 'secondary'));
    return drawer('核对交付回执', content);
  }
  const content = append(node('div', 'detail-stack'), node('p', '', '交付的是前端结果 R1 的这个准确版本，不是后来可能改变的工作区。'), node('div', 'code-pin', sha),
    node('div', 'dialog-note', '实际产品仍需现有 Git 验证门禁和准确交付回执。本原型不读写真实文件。'));
  const answer = await dialog('确认文件交付 · R1', content, [{ id: 'cancel', label: '取消' }, { id: 'publish', label: '提交交付请求 · 演示', variant: 'secondary' }]);
  if (answer === 'publish') {
    state.delivery = state.fixture === 'blocked' ? 'blocked' : 'unknown';
    render();
    toast(state.delivery === 'blocked' ? '示例门禁拒绝：缺少验证配置。下游保持等待，不启动。' : '交付请求已模拟提出；回执未知，尚未确认交付。');
    $('task-primary')?.focus();
  }
}
async function withdraw() {
  if (!['active', 'unknown'].includes(state.grant)) return;
  const answer = await dialog('撤销前端本次授权', node('div', 'detail-stack', '只影响「登录前端实现」的本次授权。不会停止你的其他对话，也不会撤回已产生费用或已经交付的文件。授权撤销与运行取消回执分别显示。'), [{ id: 'cancel', label: '保留授权' }, { id: 'withdraw', label: '撤销本次授权 · 演示', variant: 'danger' }]);
  if (answer === 'withdraw') {
    state.grant = 'withdrawn'; render(); toast('模拟授权已撤销；取消回执仍未知，未声称执行已经停止。');
  }
}
async function showEvidence() {
  const content = node('div', 'detail-stack');
  append(content, node('p', '', '三个工作线程，共同围绕「登录体验改造」。这里只查看事实，不派工。'),
    append(node('div', 'receipt-grid'), fact('方案执行', '完成 · 示例 R1'), fact('方案审阅', state.researchReviewed ? '已接受' : '未审阅'), fact('前端执行', state.result ? '完成' : status('frontend')[0]), fact('前端审阅', state.fileReviewed ? '已接受' : '未审阅'), fact('文件交付', { held: '未请求', unknown: '回执未知', blocked: '门禁拒绝', delivered: '示例回执已确认' }[state.delivery]), fact('取消回执', state.grant === 'withdrawn' ? '未知' : '不适用')));
  const versions = append(node('div', 'detail-section ui-stack'), node('h3', '', '版本与依赖'), node('p', '', '兼容检查依赖前端结果 R1 的准确交付。接受方案、接受实现或合并候选都不能替代这个证据。'), node('div', 'code-pin', sha));
  content.append(versions);
  if (['active', 'unknown'].includes(state.grant)) content.append(button('撤销前端本次授权', async () => {
    document.querySelector('dialog[open]')?.close('');
    // Wait for the native close event to settle the drawer before the next modal.
    setTimeout(withdraw, 0);
  }, 'danger'));
  await drawer('任务脉络 · 登录体验改造', content);
}
async function roleInfo() {
  const content = append(node('div', 'detail-stack'), node('h3', '', '置顶是位置，协调是角色。'),
    node('p', '', 'Coordinator 固定在当前项目对话列表顶部。打开、置顶或重命名普通对话，都不会让它自动成为协调者。'),
    fact('当前项目', '产品研发 · 示例项目'), fact('协调角色', state.bound ? 'Coordinator · 显式绑定' : '未绑定'),
    node('p', '', '真实更换角色需确认旧、新对话及当前绑定版本；旧授权不随更换或换回自动恢复。本原型不执行角色替换。'));
  await drawer('关于 Coordinator', content);
}
async function bindCoordinator() {
  const answer = await dialog('设置项目 Coordinator', node('div', 'detail-stack', '当前项目：产品研发（示例）。从未绑定状态，将这个示例对话设为 Coordinator。该动作只绑定角色，不启动任何任务。'), [{ id: 'cancel', label: '取消' }, { id: 'bind', label: '绑定示例 Coordinator', variant: 'secondary' }]);
  if (answer === 'bind') { state.bound = true; render(); toast('仅模拟角色绑定，未派工。'); }
}
async function designTools() {
  const content = node('div', 'detail-stack');
  const fixture = ui.createField({ label: '演示场景', value: state.fixture, choices: [{ value: 'normal', label: '正常路径' }, { value: 'uncertain', label: '授权回执未知' }, { value: 'blocked', label: '文件交付被门禁拒绝' }, { value: 'unbound', label: 'Coordinator 未绑定' }] });
  const theme = ui.createField({ label: '外观', value: document.documentElement.dataset.theme, choices: [{ value: 'tofu', label: 'Tofu · 暖色点睛' }, { value: 'dark', label: 'Dark · 石墨' }, { value: 'light', label: 'Light · 明亮' }] });
  content.append(node('div', 'dialog-note', '这里只改变本页示例状态。刷新页面会恢复初始原型，所有按钮均不调用后端。'), fixture.el, theme.el,
    button('应用场景并重置', () => {
      state = { view: 'coordinator', researchReviewed: false, grant: 'none', result: false, fileReviewed: false, delivery: 'held', fixture: fixture.control.value, bound: fixture.control.value !== 'unbound' };
      messages.clear(); drafts.clear(); document.documentElement.dataset.theme = theme.control.value;
      document.querySelector('dialog[open]')?.close(''); render(); toast('演示已重置。');
    }, 'secondary', 'apply-fixture'),
    button('模拟执行结果到达', () => {
      if (state.grant !== 'active') { toast('请先审阅方案并确认一次有限授权。'); return; }
      state.result = true; document.querySelector('dialog[open]')?.close(''); render(); toast('模拟执行完成，尚未审阅或交付。');
    }, 'secondary', 'demo-complete'));
  await drawer('设计预览 · 演示控制', content);
}

$('sidebar-actions').append(icon('收起对话列表', () => {
  if (window.matchMedia('(max-width:720px)').matches) {
    $('shell').dataset.mobileList = 'false'; $('main').inert = false; mobileOpener?.focus();
  } else toast('桌面保留对话列表。窄屏可收起。');
}, '<path d="M9 4v16M5 4h14v16H5zM15 9l-3 3 3 3"/>'));
$('new-conversation').append(button('＋ 新对话', () => {
  if (stories.length >= 24) { toast('原型最多保留 20 个新增对话。刷新可清空。'); return; }
  const sequence = stories.length - 3;
  const id = 'note-' + sequence;
  stories.push({ id, title: '新对话 ' + sequence, short: '普通对话', letter: '✎', detail: '本页示例 · 不参与任务', type: 'notes' });
  query = ''; search.control.value = '';
  select(id); input.focus();
  toast('已创建本页示例对话，没有新建真实会话或启动任务。');
}, 'secondary', 'new-chat', { size: 'md' }));
const search = ui.createField({ label: '搜索对话', value: '', controlId: 'conversation-search' });
search.control.placeholder = '标题或工作内容';
search.control.addEventListener('input', () => { query = search.control.value; renderSidebar(); });
$('search-slot').append(search.el);
$('design-tools').append(button('◐ 外观与演示', designTools, 'ghost', 'design-controls'), button('设计说明 ↗', roleInfo, 'ghost'));
$('mobile-navigation').append(icon('打开对话列表', () => {
  mobileOpener = document.activeElement;
  $('shell').dataset.mobileList = 'true'; $('main').inert = true;
  $('nav-coordinator').focus();
}, '<path d="M4 6h16M4 12h16M4 18h16"/>'));
// Native textarea adapter uses the kit's documented textarea skin.
const textarea = node('textarea', 'ui-input');
textarea.rows = 2;
textarea.maxLength = 4000;
const composerField = ui.createField({ label: '继续对话', control: textarea, controlId: 'message-input', hint: '' });
const input = composerField.control;
input.placeholder = '说说下一步，或把新想法留在这里…';
$('composer-field').append(composerField.el);
input.addEventListener('input', () => drafts.set(state.view, input.value));
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); addMessage(input.value); }
});
$('composer-actions').append(button('发送 ↑', () => addMessage(input.value), 'secondary', 'send-message'));
$('composer-form').addEventListener('submit', (event) => { event.preventDefault(); addMessage(input.value); });
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && $('shell').dataset.mobileList === 'true' && !modalOpen) {
    $('shell').dataset.mobileList = 'false'; $('main').inert = false; mobileOpener?.focus();
  }
});
window.matchMedia('(max-width:720px)').addEventListener('change', () => {
  $('shell').dataset.mobileList = 'false'; $('main').inert = false;
  if (!modalOpen) restoreFocus(document.activeElement);

  $('header-evidence').textContent = window.matchMedia('(max-width:720px)').matches ? '脉络' : '任务脉络';
});
render();
window.__prototypeReady = true;
