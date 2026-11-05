/* In-memory interaction study. No fetch, storage, model, real task commands,
 * analytics or production modules. One fixture backs both visual surfaces. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const fixture = { id: 'design-memory-research', state: 'waiting', pause: false, choice: '', updates: [] };
  const labels = { waiting: '待你选择实验路线', running: '正在执行实验', delivered: '成果已生成 · 待验收', interrupted: '执行中断 · 需要处理', stopped: '已停止 · 保留已有成果' };
  const tabNames = ['overview', 'literature', 'experiments', 'results', 'history'];
  let targetTask = false, currentView = 'chat', currentTab = 'overview', readerDocument = '', toastTimer;
  let savedChatScroll = 0;
  const escape = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function toast(message) {
    clearTimeout(toastTimer);
    $('#toast').textContent = message; $('#toast').hidden = false;
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3400);
  }
  function decisionMarkup() {
    if (fixture.state !== 'waiting') return '';
    return '<section class="decision"><p class="eyebrow">需要你决定</p><h2>先验证哪条路线？</h2><p>建议先做 A，可复用已有评测集；B 需要补充压缩对照。</p><div class="decision-options"><button class="button primary" data-answer="A">优先 A · 外部记忆</button><button class="button" data-answer="B">优先 B · 上下文压缩</button></div></section>';
  }
  function noticeMarkup() {
    if (fixture.state === 'waiting') return '';
    if (fixture.state === 'running') return `<strong>${fixture.choice ? `已选择路线 ${fixture.choice}` : '实验进行中'}</strong><p>后续结果将在这里更新。${fixture.pause ? '本轮收尾后暂停，不再推进下一阶段。' : '可以继续讨论或补充要求。'}</p>`;
    if (fixture.state === 'delivered') return '<strong>研究简报已生成，仍需验收。</strong><p>执行结束与目标达成分开呈现。查看结论、证据和未解决的问题。</p><button class="button" data-document="report">查看研究简报 ↗</button>';
    if (fixture.state === 'interrupted') return '<strong>执行中断，已有材料和记录仍在。</strong><p>先检查中断位置与可复用的结果，再决定是否开始新的尝试。</p><button class="button" data-tab="history" data-open-workbench="true">查看中断记录</button><button class="text-button retry-button" data-action="retry">开始新的尝试</button>';
    return '<strong>整个任务已停止。</strong><p>已有成果保留，后续阶段不再自动启动。</p><button class="text-button" data-tab="results" data-open-workbench="true">查看已保存成果 ↗</button>';
  }
  function updateMarkup(item) {
    return `<article class="update ${item.task ? '' : 'ordinary'}"><strong>${item.task ? '你 · 当前任务' : '你 · 普通对话'}</strong><p>${escape(item.text)}</p><small>${escape(item.receipt)}</small></article>`;
  }
  function render() {
    $$('.state-label').forEach(node => { node.textContent = labels[fixture.state]; });
    $$('.decision-slot').forEach(node => { node.innerHTML = decisionMarkup(); node.hidden = fixture.state !== 'waiting'; });
    $$('[data-state-notice]').forEach(node => { node.innerHTML = noticeMarkup(); node.hidden = fixture.state === 'waiting'; });
    $$('.pause-label').forEach(node => { node.hidden = !fixture.pause; });
    $('#scenario').value = fixture.state;
    $('#milestoneCaption').textContent = fixture.choice ? `已选择路线 ${fixture.choice}` : fixture.state === 'waiting' ? '等待选择实验路线' : '保留已确认的阶段记录';
    $('#reportState').textContent = fixture.state === 'delivered' ? '已生成 · 尚待验收' : '草稿 · 尚未完成实验';
    $('#chatUpdates').innerHTML = fixture.updates.map(updateMarkup).join('');
    $('#workUpdates').innerHTML = fixture.updates.filter(item => item.task).map(updateMarkup).join('') || '<p class="muted">还没有补充要求。</p>';
    $('#historyUpdates').innerHTML = fixture.updates.filter(item => item.task).map(updateMarkup).join('');
    $('#announcement').textContent = labels[fixture.state];
    $$('[data-action="pause"]').forEach(button => { button.disabled = fixture.pause || ['stopped', 'delivered', 'interrupted'].includes(fixture.state); });
    $$('[data-action="stop"]').forEach(button => { button.disabled = ['stopped', 'delivered'].includes(fixture.state); });
  }
  function setTarget(value, focus = false) {
    targetTask = value; $('#chatScope').hidden = !value;
    $('#composeMode').textContent = value ? '补充当前任务' : '普通对话';
    $('#chatInput').placeholder = value ? '补充范围、材料或下一步要求…' : '继续讨论，或为这项工作补充要求…';
    if (focus) $('#chatInput').focus();
  }
  function selectTab(name, focus = false) {
    if (!tabNames.includes(name)) return;
    currentTab = name;
    $$('.work-tabs [role="tab"]').forEach(button => {
      const selected = button.dataset.tab === name;
      button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    });
    tabNames.forEach(tab => { $(`#panel-${tab}`).hidden = tab !== name; });
    if (focus) $(`#tab-${name}`).focus();
  }
  function displayView(view) {
    if (view !== 'chat' && view !== 'workbench') return;
    if (currentView === 'chat') savedChatScroll = $('#chatScroll').scrollTop;
    currentView = view;
    $('#newWorkLabel').textContent = view === 'chat' ? '新对话' : '新目标';
    $('.new-work').dataset.action = view === 'chat' ? 'demo' : 'new';
    $('#chatView').hidden = view !== 'chat'; $('#workbenchView').hidden = view !== 'workbench';
    $$('.list-tabs [data-view], .feature-nav [data-view]').forEach(button => {
      button.classList.toggle('selected', button.dataset.view === view);
    });
    if ($('#navigation').open) $('#navigation').close();
    if (view === 'chat') $('#chatScroll').scrollTop = savedChatScroll;
    document.title = `Tofu · ${view === 'chat' ? '任务对话' : '任务工作台'} · 设计稿`;
  }
  function navigate(view) {
    if (location.hash === `#${view}`) displayView(view); else location.hash = view;
  }
  const documents = {
    comparison: { title: '文献与方法对照', content: '<h3>比较的是失效边界</h3><p>外部记忆关注能否检索到相关片段；上下文压缩关注哪些细节会在压缩时丢失。</p><blockquote>同样的平均分，可能掩盖完全不同的失败问题。应先检查具体的证据缺失，再比较效率。</blockquote><h3>来源与限制</h3><p>材料 A、材料 B 为设计稿示例。这里展示出处、相关原文和适用边界的位置，不构成真实文献证据。</p><h3>下一步</h3><p>固定问题集、基线和评分方式，用同一组跨文档问题检验候选方法。</p>' },
    protocol: { title: '候选实验草案', content: '<h3>路线 A · 外部记忆</h3><p>复用同一组问题，保留无检索的基线，记录检索到的片段与回答依据。</p><h3>路线 B · 上下文压缩</h3><p>固定压缩强度，逐项检查关键证据是否保留，与原始上下文比较。</p><h3>判断规则</h3><p>同时记录成功与失败案例。没有足够证据时保留“不确定”，不把一次执行完成当作假设成立。</p><p class="caption">示例草案，未运行真实实验。</p>' },
    report: { title: '研究简报', content: '<h3>当前结论</h3><p>先检验信息召回边界，再比较质量与成本。这个结论在本设计稿中仅用于演示成果阅读形式。</p><h3>已形成的产物</h3><p>方法对照、候选方向、实验草案，以及逐项验收的位置。</p><h3>仍未解决</h3><p>尚无真实实验结果；无法据此宣称任何方法有效、领先或具有科学新颖性。</p>' },
    scope: { title: '目标与范围', content: '<h3>原始目标</h3><p>比较长上下文记忆方案，形成值得验证的方向，再做实验。</p><h3>交付与验收</h3><p>结论关联来源，实验保留配置与结果，对无法判断的问题明确说明。</p><h3>后续补充</h3><p>补充要求先显示已接收，再由执行方确认采纳。未采纳的消息不会被标记为已生效。</p>' },
    coordinator: { title: '项目负责人 · 工作概览', content: '<h3>当前项目的三项工作</h3><p>长上下文记忆研究：需要选择实验路线。</p><p>梳理评测数据集：已有阶段报告。</p><p>复现基线结果：已结束，可查看记录。</p><h3>继续沿用现有 Coordinator</h3><p>项目汇报保留在负责人对话；选中具体工作后展开任务与成果。此处仅演示简报层级。</p>' },
  };
  function openDocument(key) {
    const entry = documents[key]; if (!entry) return;
    readerDocument = key;
    $('#readerType').textContent = key === 'coordinator' ? '项目简报 · 示例' : '当前任务 · 示例产物';
    $('#readerBody').innerHTML = `<p class="eyebrow">长上下文记忆研究</p><h2>${entry.title}</h2>${entry.content}`;
    $('#reader footer').hidden = key === 'coordinator';
    if (!$('#reader').open) $('#reader').showModal();
  }
  function record(text, receipt, task = true) { fixture.updates.push({ text, receipt, task }); render(); }
  function answer(choice) {
    if (fixture.state !== 'waiting' || !['A', 'B'].includes(choice)) return;
    fixture.choice = choice; fixture.state = 'running';
    record(`优先验证路线 ${choice}：${choice === 'A' ? '外部记忆' : '上下文压缩'}。`, '选择已接收 · 两个视图同步');
    toast('选择已同步到任务，工作台与对话中显示同一条记录。');
  }
  document.addEventListener('click', event => {
    const control = event.target.closest('button'); if (!control || control.disabled) return;
    if (control.dataset.view) { navigate(control.dataset.view); return; }
    if (control.dataset.document) { openDocument(control.dataset.document); return; }
    if (control.dataset.tab) { if (control.dataset.openWorkbench) navigate('workbench'); selectTab(control.dataset.tab); return; }
    if (control.dataset.answer) { answer(control.dataset.answer); return; }
    if (control.dataset.template) {
      $('#objective').value = control.dataset.template === 'literature' ? '比较长上下文记忆方向的论文，整理证据与值得验证的方案。' : '验证现有方案在跨文档问题上的表现，保留基线、实验记录和失败案例。';
      $('#objective').focus(); return;
    }
    switch (control.dataset.action) {
      case 'attention': $('#chatScroll').scrollTop = $('#chatScroll').scrollHeight; break;
      case 'target': setTarget(true, true); break;
      case 'clear-target': setTarget(false, true); break;
      case 'current': navigate(currentView); break;
      case 'scope': openDocument('scope'); break;
      case 'paper': openDocument('comparison'); break;
      case 'coordinator': openDocument('coordinator'); break;
      case 'new': if ($('#navigation').open) $('#navigation').close(); $('#newTask').showModal(); break;
      case 'close-new': $('#newTask').close(); break;
      case 'close-reader': $('#reader').close(); break;
      case 'navigation': $('#navigation').showModal(); break;
      case 'close-nav': $('#navigation').close(); break;
      case 'reader-to-workbench': $('#reader').close(); navigate('workbench'); selectTab(readerDocument === 'protocol' ? 'experiments' : readerDocument === 'scope' ? 'overview' : 'results'); break;
      case 'quote': {
        const title = documents[readerDocument]?.title || '所选内容'; $('#reader').close();
        const input = currentView === 'chat' ? $('#chatInput') : $('#workInput');
        if (currentView === 'chat') setTarget(true);
        input.value = `关于《${title}》：${input.value}`; input.focus(); break;
      }
      case 'pause': fixture.pause = true; record('本轮结束后暂停推进。', '已记录 · 当前阶段继续收尾'); toast('当前阶段继续，之后不再推进新阶段。'); break;
      case 'stop': fixture.state = 'stopped'; fixture.pause = false; record('停止整个任务。', '演示状态：已停止 · 保留已有成果'); break;
      case 'retry': fixture.state = 'running'; record('从已保存的成果开始一次新的尝试。', '新尝试 · 原中断记录保留'); break;
      case 'demo': toast('本设计稿重点演示“长上下文记忆研究”的完整路径。'); break;
    }
    if (['pause', 'stop'].includes(control.dataset.action)) $$('.control-menu').forEach(menu => { menu.open = false; });
  });
  $('#chatComposer').addEventListener('submit', event => {
    event.preventDefault(); const text = $('#chatInput').value.trim(); if (!text) return;
    record(text, targetTask ? '已记录 · 等待执行者采纳' : '普通对话 · 未改变任务要求', targetTask);
    $('#chatInput').value = ''; $('#chatScroll').scrollTop = $('#chatScroll').scrollHeight;
  });
  $('#workComposer').addEventListener('submit', event => {
    event.preventDefault(); const text = $('#workInput').value.trim(); if (!text) return;
    record(text, '已记录 · 等待执行者采纳'); $('#workInput').value = '';
    toast('补充已记录；回到对话也可以看到这条消息。');
  });
  $('#newTaskForm').addEventListener('submit', event => {
    event.preventDefault(); const objective = $('#objective').value.trim(); if (!objective) return;
    $('#newTask').close(); readerDocument = '';
    $('#readerType').textContent = '创建流程 · 设计预览';
    $('#readerBody').innerHTML = `<p class="eyebrow">自动科研</p><h2>准备开始的目标</h2><p>${escape(objective)}</p><h3>范围与验收</h3><p>${escape($('#acceptance').value.trim() || '执行前整理范围与交付要求。')}</p><h3>接下来</h3><p>真实接入后，在任务确认创建时返回同一条任务入口。此设计预览到此结束，没有启动执行，也没有继承其他任务的材料。</p>`;
    $('#reader footer').hidden = true; $('#reader').showModal();
  });
  $('#scenario').addEventListener('change', event => {
    fixture.state = event.target.value; fixture.choice = ''; fixture.pause = false; render();
    toast('已切换设计示例状态。');
  });
  $('#reset').addEventListener('click', () => {
    Object.assign(fixture, { state: 'waiting', pause: false, choice: '', updates: [] });
    $$('.composer textarea').forEach(input => { input.value = ''; }); setTarget(false); selectTab('overview'); render();
  });
  $('.work-tabs').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const index = tabNames.indexOf(currentTab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabNames.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabNames.length) % tabNames.length;
    selectTab(tabNames[next], true);
  });
  window.addEventListener('hashchange', () => displayView(location.hash === '#workbench' ? 'workbench' : 'chat'));
  displayView(location.hash === '#workbench' ? 'workbench' : 'chat'); render();
  // Follow the initial latest message once; later renders never steal reading position.
  document.fonts.ready.then(() => {
    if (currentView === 'chat') $('#chatScroll').scrollTop = $('#chatScroll').scrollHeight;
  });
})();
