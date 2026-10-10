/* Research workbench DESIGN STUDY only: synthetic data, memory-only state,
 * no fetch, production imports, local persistence, real jobs or remote effects. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const all = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const pages = ['home', 'brief', 'evidence', 'hypotheses', 'experiments', 'outputs'];
  const scenarios = ['synthesis', 'initial', 'approval', 'disconnected', 'interrupted', 'stale', 'complete'];
  const state = { page: 'brief', scenario: 'synthesis', selected: null, sourceScope: new Set(['A','B','C','D']), scopeDirty: false, approved: false, filter: '', relation: 'all', goal: '验证一个判断', question: '', drafts: {}, deferred: false
, contextOpen: true, initialDraft: false
, evidenceView: 'literature', sort: 'relevance', venueFilter: 'all', topVenues: new Set(['ICLR','ICML','NeurIPS'])
 };
  // Bibliographic fixtures are NOT fetched records. Null means unavailable, never zero.
  const bibliography = {
    A: { publicationYear: 2025, yearKind: '正式发表', preprintYear: 2024, citationCount: 128, citationProvider: 'Semantic Scholar', citationCheckedAt: '2026-09-07', publicationStatus: 'accepted', venue: 'ICLR', track: 'main', venueCheckedAt: '2026-09-07', venueEvidence: '官方录用决定 / 合成快照 A', metadataRecord: 'demo-bibliography-A@v1', relevanceRank: 1 },
    B: { publicationYear: 2025, yearKind: 'Workshop 发表', preprintYear: 2025, citationCount: 1204, citationProvider: 'Semantic Scholar', citationCheckedAt: '2026-09-07', publicationStatus: 'accepted', venue: 'NeurIPS', track: 'workshop', venueCheckedAt: '2026-09-06', venueEvidence: 'Workshop 论文集 / 合成快照 B', metadataRecord: 'demo-bibliography-B@v1', relevanceRank: 2 },
    C: { publicationYear: 2024, yearKind: '预印本首发', preprintYear: 2024, citationCount: 0, citationProvider: 'Semantic Scholar', citationCheckedAt: '2026-09-07', publicationStatus: 'not_found', venue: null, track: 'preprint', venueCheckedAt: '2026-09-07', venueEvidence: '本次目录查询未匹配 / 合成快照 C；不代表被拒稿', metadataRecord: 'demo-bibliography-C@v1', relevanceRank: 3 },
    D: { publicationYear: null, yearKind: '未知', preprintYear: null, citationCount: null, citationProvider: 'Semantic Scholar', citationCheckedAt: null, publicationStatus: 'unknown', venue: null, track: null, venueCheckedAt: null, venueEvidence: '身份尚未匹配，无法核验录用状态', metadataRecord: 'demo-bibliography-D@v1', relevanceRank: 4 }
  };
  const venueOptions = ['ICLR','ICML','NeurIPS','ACL','EMNLP','CVPR','ICCV','ECCV'];
  const sortLabels = { relevance:'研究相关性', 'year-desc':'年份：新 → 旧', 'year-asc':'年份：旧 → 新', 'citations-desc':'引用：高 → 低', 'citations-asc':'引用：低 → 高' };
  const sourceData = [
    {id:'A', title:'结构化反馈与工具错误修复', relation:'支持机制', type:'support', detail:'反馈包含字段位置、错误类型与可修复约束。', quote:'结构化反馈使模型能够定位失败的参数字段；但这一比较未把修复轮次的额外生成成本纳入共同预算。', limitation:'支持定位机制；不支持“等预算更优”的结论。', location:'示例文献 A / §4.2 / 段落 3', coverage:'节选可用', read:'节选核验', scope:'工具参数错误', budget:'未匹配', locator:'demo-source-A@v1 · chars 240–298', basis:'演示：引文位置与版本已保留；语义支持范围仍需人工复核。'},
    {id:'B', title:'推理预算匹配后的反思对照', relation:'限制结论', type:'challenge', detail:'额外反思轮次和基线重试使用相同生成配额。', quote:'在匹配生成预算后，平均提升不再一致。结果随错误类型变化，部分任务中重试基线表现接近反思策略。', limitation:'不能把不匹配预算的提升归因于恢复机制本身。', location:'示例文献 B / §5.1 / 段落 2', coverage:'节选可用', read:'节选核验', scope:'多轮推理错误', budget:'仅匹配 tokens', locator:'demo-source-B@v2 · chars 512–563', basis:'演示：存在竞争解释；数据集和错误分布仍不同。'},
    {id:'C', title:'多轮工具轨迹中的级联失败', relation:'定义指标', type:'context', detail:'将首次工具失败后的后续依赖错误单独统计。', quote:'端到端成功率会掩盖错误传播路径。我们同时记录首次失败后的恢复率、额外调用成本及后续依赖错误的数量。', limitation:'定义评测维度，不构成具体恢复策略的效果证据。', location:'示例文献 C / §3 / 段落 4', coverage:'节选可用', read:'节选核验', scope:'级联失败轨迹', budget:'仅报告成本', locator:'demo-source-C@v1 · chars 90–144', basis:'演示：指标定义相关；尚未验证指标对当前任务的适用性。'},
    {id:'D', title:'检查点回滚的检索线索', relation:'尚未核验', type:'unread', detail:'仅保留元信息；全文未取得，不参与支持性论证。', quote:'此来源只有元信息，没有可展示的原文证据。', limitation:'不得从标题推断实验方法、结果或新颖性。', location:'示例文献 D / 元信息记录', coverage:'全文不可用', read:'仅元信息', scope:'待确认', budget:'未知', locator:'demo-source-D@v1 · metadata only', basis:'演示：需要取得原文后重新判断；不可视为已读。'}
  ].map(source => ({...source, ...bibliography[source.id]}));
  const main = $('#main');
  const context = $('#context');
  const modal = $('#modal');
  let dialogTrigger = null;
  let inspectorTrigger = null;
  let toastTimer;
  const badge = (label, color='gray') => `<span class="badge ${color}">${label}</span>`;
  const cite = (id, label=id) => `<button class="cite" data-source="${id}" aria-label="查看示例证据 ${id}">${label} ↗</button>`;
  const button = (text, action, primary=false) => `<button class="button${primary?' primary':''}" data-action="${action}">${text}</button>`;
  const pageHeading = (kicker, title, description='') => `<div class="page-kicker"><span class="eyebrow">${kicker}</span><span class="scenario-tag">DEMO / v0.1</span></div><h1>${title}</h1>${description?`<p class="page-subtitle">${description}</p>`:''}`;
  const question = '小参数模型多轮工具调用中的错误恢复：哪种自我修正机制能在不增加推理预算的情况下降低级联失败？';
  const constraints = () => `<div class="constraint-row"><span class="constraint"><span>◇</span>1–8B 参数范围 · 假设</span><span class="constraint"><span>≋</span>等 tokens + 工具调用上限</span><span class="constraint"><span>⊥</span>优先寻找反例</span></div>`;

  function briefPage() {
    if (state.scenario === 'initial') return initialPage();
    const stale = state.scenario === 'stale' || state.scopeDirty;
    return pageHeading('RESEARCH BRIEF / 研究简报', '先定位错误，再决定如何恢复。')
      + `<details class="question-details"><summary>查看完整研究问题与范围</summary><p>${escape(state.question || question)}</p><p class="micro">以上范围为可修改的研究假设；“等预算”必须在实验前明确定义。</p></details>`
      + constraints()

      + `${needsDecision() ? '<div class="mobile-action"><span>1 项需要你判断</span>'+button(needsRevalidation()?'查看复核计划':'审阅实验草案',needsRevalidation()?'recheck':'approve',true)+'</div>' : ''}`
      + `<section class="judgment" aria-labelledby="judgment-title"><span class="eyebrow">${stale?'需要重新核验 · 旧版判断':'当前判断 · 示例，尚待实验'}</span><h2 id="judgment-title">值得先检验的不是“多想一轮”，<br>而是“在同一预算内，修复得更有针对性”。</h2><p>优先对比结构化错误反馈与普通重试。现有示例证据提示：额外推理成本是必须排除的竞争解释，不能直接归因为恢复机制的效果。${cite('A')}${cite('B')}</p><div class="judgment-footer">${badge(stale?'依赖已变化':'暂定假设',stale?'amber':'violet')}<span>支持范围：实验设计依据 · 不代表策略已被验证</span></div></section>`
      + `<div class="section-heading"><h2>证据告诉了我们什么</h2><button class="text-button" data-page="evidence">查看比较表 →</button></div><div class="finding-list"><article class="finding"><span class="finding-number">01</span><div><h3>结构化反馈可能改善错误定位 ${cite('A')}</h3><p>能否转化为等预算下的收益，还需要匹配对照。</p></div>${badge('机制线索','green')}</article><article class="finding"><span class="finding-number">02</span><div><h3>预算不匹配，会混淆机制与计算量 ${cite('B')}</h3><p>额外 tokens、重试次数与工具调用成本需要分开报告。</p></div>${badge('竞争解释','amber')}</article><article class="finding"><span class="finding-number">03</span><div><h3>只看总成功率，会漏掉错误传播 ${cite('C')}</h3><p>同时观察恢复率、级联长度与成本，而不是只选一个好看的指标。</p></div>${badge('评测约束')}</article></div>`
      + `<section class="next-step"><div class="next-step-head"><span class="step-icon" aria-hidden="true">↗</span><div><span class="eyebrow">最小下一步</span><h3>用一组配对轨迹，区分机制收益与预算收益。</h3></div></div><p>同一模型、同一错误注入、同一预算上限；先做先导实验，再决定是否扩大。若效果消失，保留反证，不追加无理由的计算。</p><div class="action-row"><button class="button primary" data-page="experiments">查看实验草案 <span aria-hidden="true">→</span></button><span class="micro">未获批准前，不提交计算任务</span></div></section>`;
  }

  function initialPage() {

    if(state.question) return pageHeading('RESEARCH / 新问题草稿', '先把问题定义清楚。', '已接收问题。下一步应识别目标、范围与可验证的交付物。')
      + `<section class="empty-state"><span class="eyebrow">你的问题</span><h2>${escape(state.question)}</h2><p>预期产物：${escape(state.goal)}。这个离线原型不会进行检索或生成实际研究计划。</p><div class="note-box">研究对象、对照、评价标准与资源需求：尚待解析。不会自动套用错误恢复示例的范围或协议。</div></section><div class="section-heading"><h2>下一步会做什么</h2></div><ol class="plan-list"><li>解析目标和已给出的硬约束</li><li>检查已有来源与可复用材料</li><li>提出可审阅的计划<small>只在关键歧义会改变工作时询问</small></li></ol>`;
    return pageHeading('RESEARCH BRIEF / 已开始', '先把问题定义清楚。', '正在建立证据范围。此时没有可靠结论，也不会用占位文字冒充研究进展。')
      + `<section class="empty-state"><span class="eyebrow">我理解的目标</span><h2>${escape(state.question || '找出等推理预算下，能减少级联失败的恢复机制。')}</h2><p>先获得一个可检验的比较方案，再决定是否运行实验。默认不执行外部计算，不发布任何内容。</p>${constraints()}<details class="question-details"><summary>什么还需要明确？</summary><p>将“预算”暂定为总生成 tokens 与工具调用次数的共同上限，延迟单独报告。如果你的约束不同，可以修改计划。</p></details></section><div class="section-heading"><h2>第一个有用产物</h2>${badge('不是最终结论')}</div><ol class="plan-list"><li>建立可比较的证据表<small>来源、错误类型、预算定义、评测指标和限制。</small></li><li>标出仍然成立的竞争解释<small>尤其检查是否只是多用了计算或更换了任务分布。</small></li><li>给出一个最小可证伪实验<small>预先写清什么结果会让我们放弃假设。</small></li></ol><div class="note-box"><strong>检索正在准备 · 演示状态</strong><br>没有语义进度回执前，不显示“已读完”或虚构百分比。</div><div class="action-row">${button('修改研究范围','scope')}<span class="micro">可以离开页面；真实产品应保留已确认的检查点。</span></div>`;
  }

  function isTopVenue(source) {
    return source.publicationStatus === 'accepted' && source.track === 'main' && state.topVenues.has(source.venue);
  }
  function venueLabel(source) {
    if (source.publicationStatus === 'accepted') return `${source.venue} · ${source.track === 'main' ? '主会' : 'Workshop'}`;
    return source.publicationStatus === 'not_found' ? '未发现录用记录' : '待核验';
  }
  function visibleSources() {
    const query = state.filter.trim().toLocaleLowerCase();
    const rows = sourceData.filter(source =>
      (state.relation === 'all' || source.type === state.relation) &&
      (state.venueFilter === 'all' || (state.venueFilter === 'top' ? isTopVenue(source) : state.venueFilter === 'workshop' ? source.track === 'workshop' : source.publicationStatus !== 'accepted')) &&
      `${source.title} ${source.scope} ${source.limitation} ${source.venue || ''} ${source.publicationYear || ''}`.toLocaleLowerCase().includes(query));
    const [key, direction] = state.sort.split('-');
    const field = key === 'year' ? 'publicationYear' : key === 'citations' ? 'citationCount' : 'relevanceRank';
    return rows.sort((a, b) => {
      const av = a[field], bv = b[field];
      if (av === null && bv !== null) return 1;
      if (bv === null && av !== null) return -1;
      const difference = av === null ? 0 : (av - bv) * (direction === 'desc' ? -1 : 1);
      return difference || a.title.localeCompare(b.title, 'zh-CN') || a.id.localeCompare(b.id);
    });
  }
  function sortHeading(key, label) {
    const active = state.sort.startsWith(key + '-');
    const ascending = state.sort.endsWith('-asc');
    return `<th scope="col" class="numeric" data-sort-column="${key}"${active ? ` aria-sort="${ascending ? 'ascending' : 'descending'}"` : ''}><button class="sort-button" data-sort-key="${key}" aria-label="${label}：${active && !ascending ? '升序' : '降序'}排列">${label} <span aria-hidden="true">${active ? ascending ? '↑' : '↓' : '↕'}</span></button></th>`;
  }
  function tableHead() {
    if (state.evidenceView === 'comparison') return '<tr><th scope="col">纳入</th><th scope="col">来源与证据角色</th><th scope="col">预算可比性</th><th scope="col">原文覆盖</th><th scope="col">对判断的限制</th></tr>';
    return `<tr><th scope="col">纳入</th><th scope="col" class="paper-column">论文 / 与问题的关系</th>${sortHeading('year','年份')}<th scope="col">会议与录用</th>${sortHeading('citations','引用数')}<th scope="col">阅读状态</th></tr>`;
  }
  function evidencePage() {
    const literature = state.evidenceView === 'literature';
    return pageHeading('EVIDENCE / 证据与文献', '文献，放在一起比较。', '先看与问题的关系，再看发表记录与影响力。顶会与高引用，都不能代替证据本身。')
      + `<div class="literature-summary"><span><strong>4</strong> 条演示来源</span><span>3 条节选 · 1 条仅元信息</span><span>全部为合成数据 · 无在线核验</span></div>
      <div class="view-switch" role="group" aria-label="文献视图"><button data-evidence-view="literature" aria-pressed="${literature}">文献列表</button><button data-evidence-view="comparison" aria-pressed="${!literature}">证据比较</button><span>同一组来源，不重复入库</span></div>
      <div class="literature-toolbar"><label class="search-field"><span class="sr-only">筛选示例文献</span><input class="input" id="evidence-filter" type="search" aria-label="筛选示例文献" placeholder="搜索标题、会议或研究关系…" value="${escape(state.filter)}"></label><label>会议<select id="venue-filter" aria-label="按会议记录筛选"><option value="all">全部会议记录</option><option value="top">已核验顶会主会</option><option value="workshop">Workshop</option><option value="unverified">录用尚未核实</option></select></label><label>角色<select id="relation-filter" aria-label="按证据角色筛选"><option value="all">全部证据角色</option><option value="support">支持机制</option><option value="challenge">限制结论</option><option value="context">定义指标</option><option value="unread">尚未核验</option></select></label><label>排序<select id="paper-sort" aria-label="文献排序">${Object.entries(sortLabels).map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select></label></div>
      <div class="table-tools"><p id="paper-results">${visibleSources().length} / 4 条 · ${sortLabels[state.sort]}</p><div>${button('顶会范围','venue-policy')}${button('字段定义','columns')}</div></div>
      <p id="table-help" class="micro">${literature ? '可点击年份、引用列表头排序；未知值始终置底。' : '比较视图沿用上方排序；相同字段不代表研究可直接比较。'}窄屏可横向滚动，或选择标题查看完整详情。</p>
      <div class="table-wrap literature-table" tabindex="0" role="region" aria-label="可横向滚动的证据比较表" aria-describedby="table-help"><table><caption>${literature ? '文献列表' : '证据比较'} · 合成演示数据，非真实发表与引用记录</caption><thead id="evidence-head">${tableHead()}</thead><tbody id="evidence-rows">${evidenceRows()}</tbody></table></div>
      <div class="table-footer"><span id="scope-count">${state.sourceScope.size} 条在当前选择中</span><span>引用：Semantic Scholar 字段示例 · 快照 2026-09-07</span></div>
      <div id="scope-draft" ${state.scopeDirty?'':'hidden'} class="note-box"><strong>来源范围已修改，简报尚未重新核验。</strong><p>保留旧版，不把选择变化当作已更新的科学结论。</p><div class="action-row">${button('查看重新核验计划','recheck',true)}${button('还原来源选择','reset-scope')}</div></div>
      <details class="literature-notes"><summary>如何理解这些字段与当前证据缺口？</summary><p>年份优先正式发表年；只有预印本时显示首发年并标注。会议与引用记录不证明全文已读，未发现录用记录不等于被拒稿。示例仍缺完全匹配总预算的直接对照，D 缺可核验原文。</p><p>排序和筛选只改变浏览，不改变纳入范围或研究结论。</p></details>`;
  }
  function evidenceRows() {
    const rows = visibleSources();
    if (!rows.length) return `<tr><td colspan="${state.evidenceView === 'literature' ? 6 : 5}" class="table-empty"><strong>没有匹配的示例文献</strong><p>尝试其他关键词或清除筛选；来源选择不会被删除。</p><button class="text-button" data-action="clear-filter">清除筛选</button></td></tr>`;
    return rows.map(source => {
      const selection = `<td><input type="checkbox" data-scope="${source.id}" aria-label="将示例文献 ${source.id} 纳入来源范围" ${state.sourceScope.has(source.id)?'checked':''}></td>`;
      const title = `<td class="paper-title"><button class="text-button paper-link" data-source="${source.id}">${escape(source.title)} <span aria-hidden="true">↗</span></button><p>示例 ${source.id} · ${source.scope}</p><span class="paper-relation">${source.relation} · ${source.detail}</span></td>`;
      const cells = state.evidenceView === 'comparison'
        ? `<td>${source.budget}</td><td>${source.read}</td><td>${source.limitation}</td>`
        : `<td class="numeric"><span>${source.publicationYear ?? '—'}</span><small>${source.yearKind}</small></td><td><button class="metadata-link" data-metadata="${source.id}" aria-label="查看示例 ${source.id} 的会议与引用依据">${venueLabel(source)}</button><small>${source.publicationStatus === 'accepted' ? isTopVenue(source) ? '已核验 · 顶会范围内' : source.track === 'workshop' ? '已核验 · 非主会' : '已核验 · 不在当前顶会范围' : source.publicationStatus === 'not_found' ? '预印本 · 不代表拒稿' : '未知 · 不猜测'}</small></td><td class="numeric"><button class="metadata-link citation-number" data-metadata="${source.id}" aria-label="查看示例 ${source.id} 的引用数依据">${source.citationCount === null ? '—' : source.citationCount.toLocaleString('en-US')}</button>${source.citationCount === null ? '<small>未取得</small>' : ''}</td><td>${source.read}<small>${source.coverage}</small></td>`;
      return `<tr data-paper-id="${source.id}"${state.selected === source.id?' class="selected"':''}>${selection}${title}${cells}</tr>`;
    }).join('');
  }
  function refreshEvidence() {
    $('#evidence-head').innerHTML = tableHead();
    $('#evidence-rows').innerHTML = evidenceRows();
    $('#paper-sort').value = state.sort;
    const message = `${visibleSources().length} / 4 条 · ${sortLabels[state.sort]}`;
    $('#paper-results').textContent = message;
    announce(message + '；浏览不会修改来源选择。');
  }
  function metadataDetails(source) {
    return `<p class="metadata-warning"><strong>合成元数据 · 无在线核验</strong><br>以下数字、会议与核验回执仅演示产品行为，不是这些示例论文的真实发表记录。</p><dl class="metadata-details"><dt>年份</dt><dd>${source.publicationYear ?? '—'} · ${source.yearKind}<br>预印本首发：${source.preprintYear ?? '未知'}</dd><dt>会议与录用</dt><dd>${venueLabel(source)}<br>${source.venueEvidence}<br>核验日期：${source.venueCheckedAt ?? '尚未核验'}</dd><dt>顶会归类</dt><dd>${isTopVenue(source) ? '符合当前范围：已核验主会' : '未标为顶会主会；不等于论文质量低'}<br>由当前会议范围与 track 共同决定；Workshop 不继承主会标签。</dd><dt>引用数</dt><dd>${source.citationCount === null ? '未知，不能当作 0' : source.citationCount.toLocaleString('en-US')}<br>${source.citationProvider} 字段示例 · 取得日期：${source.citationCheckedAt ?? '未取得'}<br>仅比较同一来源快照，不相加不同数据库计数。</dd><dt>来源快照</dt><dd><code>${source.metadataRecord}</code><br>离线演示没有可打开的外部核验链接。生产需绑定原始记录、标识与证据 URL。</dd></dl>`;
  }
  function hypothesesPage() {
    const hypotheses = [
      ['H1','结构化错误反馈','优先检验','green','定位失败的字段或前置条件，比泛泛反思更节省恢复预算。','错误定位质量，而非多一次生成。','等预算下恢复率没有差异，或收益仅来自更多工具调用。','同轨迹、同错误，结构化反馈 vs 普通重试。'],
      ['H2','局部回滚与重放','证据待补','amber','恢复到最近可靠状态，避免错误继续污染后续操作。','状态污染控制，而非语言层面的解释。','回滚成本抵消恢复收益，或无污染任务同样获益。','先确认状态可回滚及其真实成本，再安排执行。'],
      ['H0','收益主要来自额外计算','必须保留的对照','violet','某些恢复方法的提升可以被相同预算的重试解释。','计算量与机会次数，而非恢复机制本身。','匹配预算后结构化机制仍有稳定、可复现的差异。','将强重试基线纳入每个实验，不只对比“不恢复”。']
    ];
    return pageHeading('HYPOTHESES / 候选解释', '让竞争解释同时留在桌面上。', '不做“创新性排行榜”。比较可区分的机制、支持范围，以及什么结果会推翻它。')
      + `<div class="note-box">以下为<strong>待验证的示例假设</strong>，不是当前模型已发现的研究结果。优先级是可解释的设计建议，不是校准后的成功概率。</div>`
      + hypotheses.map((h,i)=>`<article class="hypothesis"><div class="hypothesis-top"><span class="hypothesis-id">${h[0]}</span>${badge(h[2],h[3])}</div><h2>${h[1]}</h2><p>${h[4]}</p><dl><dt>关键区别</dt><dd>${h[5]}</dd><dt>反证条件</dt><dd>${h[6]}</dd><dt>最小测试</dt><dd>${h[7]}</dd></dl><div class="action-row">${i===0?'<button class="button primary" data-page="experiments">查看 H1 × H0 实验 →</button>':button('查看为什么保留','hypothesis-'+h[0])}${i===0?cite('A','依据 A')+cite('B','限制 B'):''}</div></article>`).join('');
  }
  function experimentsPage() {
    return pageHeading('EXPERIMENTS / 实验与判断', '批准的是一个具体实验。', '协议、预算和证据规则先固定。任务执行成功，并不等于假设得到支持。')
      + `<div class="constraint-row">${badge('H1 × H0','violet')}${badge('协议 v3 · 示例快照')}${badge(state.approved?'已模拟排队 · 尚无结果':'草案 · 未提交',state.approved?'green':'amber')}</div><section class="protocol"><div class="protocol-head"><div><span class="eyebrow">EXP-01 / 最小区分实验</span><h2>结构化反馈 vs 等预算重试</h2></div>${badge('先导实验')}</div><dl class="protocol-body"><div class="protocol-row"><dt>研究对象</dt><dd>同一小参数模型，24 条配对轨迹，覆盖参数错误、状态错误与依赖错误。样本量仅用于先导，不承诺统计效力。</dd></div><div class="protocol-row"><dt>预算匹配</dt><dd>每条轨迹总生成上限 8k tokens、最多 6 次工具调用；不使用的恢复配额也允许基线使用。延迟单独报告。</dd></div><div class="protocol-row"><dt>主要指标</dt><dd>首次错误后的任务恢复率。次要指标：级联长度、总成本、延迟、非目标行为。</dd></div><div class="protocol-row"><dt>判断规则</dt><dd>先看配对差异与不确定性，再决定验证集规模。区间跨零或覆盖不足时记为“无法区分”，不直接判赢。</dd></div><div class="protocol-row"><dt>验证约束</dt><dd>冻结提示、模型版本与错误注入；预留独立验证集。探索性分析与确认性检验分开记录。</dd></div><div class="protocol-row"><dt>执行范围</dt><dd><code>demo-eval / recovery-pilot</code> · 只写示例结果目录，不发布、不改动原数据。</dd></div><div class="protocol-row"><dt>操作预算</dt><dd>示例上限 US$3.00 / 20 分钟；达到任一上限停止新增工作。该预算与实验公平性约束分开。</dd></div></dl><div class="protocol-footer"><div class="action-row" style="margin-top:0">${state.approved?button('查看排队回执','receipt'):button('审阅并批准实验','approve',true)}${button('修改协议','edit-protocol')}<span class="micro">没有真实执行能力 · 仅演示确认流程</span></div></div></section><div class="section-heading"><h2>结果与判断</h2>${badge('尚无实验结果')}</div><div class="note-box"><strong>执行状态：</strong>${state.approved?'模拟排队':'未启动'}<br><strong>科学结果：</strong>未知<br><strong>有效性：</strong>尚未核验<br>只有真实结果产物及其回执，才能支持下一次科学判断。</div>`;
  }
  function outputsPage() {
    return pageHeading('OUTPUTS / 研究产物', '可交接，也可继续追问。', '研究的价值不只是一篇论文。保留判断、限制、反证和复现路径。')
      + `<div class="note-box"><strong>当前可交付：</strong>问题定义、证据比较与待验证实验草案。<br>没有实验结果，因此不能生成声称实验有效的论文结论。</div><div class="output-row"><span class="output-icon">MD</span><div><h3>研究决策简报 ${badge('示例草案')}</h3><p>当前判断、依据、限制与最小下一步。</p></div>${button('预览简报','brief-preview')}</div><div class="output-row"><span class="output-icon">CSV</span><div><h3>证据比较表 ${badge('3 节选 / 1 元信息')}</h3><p>保留来源状态与提取规则，不把未知字段填成零。</p></div><button class="button" data-page="evidence">打开证据表</button></div><div class="output-row"><span class="output-icon">YML</span><div><h3>实验协议与复现清单 ${badge('协议 v3')}</h3><p>假设、预算、判断规则、环境与产物要求。</p></div><button class="button" data-page="experiments">查看协议</button></div><div class="output-row"><span class="output-icon">TEX</span><div><h3>论文草稿 ${badge('结果章节未就绪','amber')}</h3><p>可以组织研究问题与方法；不得编造结果填满论文。</p></div>${button('查看就绪条件','readiness')}</div><div class="section-heading"><h2>什么会让这些产物过期？</h2></div><p class="page-subtitle">来源版本、协议或运行证据变化时，关联判断必须重新核验。PDF 编译通过只说明文件可构建，不能代替科学有效性。</p><div class="action-row">${button('预览依赖变化','stale-demo')}</div>`;
  }
  function homePage() {
    return `<div class="home-intro"><span class="eyebrow">YOUR RESEARCH / 研究首页</span><h1>把一个问题，<br>推进到有依据的下一步。</h1><p class="page-subtitle">不用先设计一条流水线。描述目标，系统准备计划，你掌握关键决定。</p></div><form id="new-form" class="new-research"><label for="new-question">你想弄清楚什么？</label><textarea id="new-question" maxlength="2000" placeholder="一个未解决的问题、一篇想复现的论文，或一个值得验证的判断…">${escape(state.question)}</textarea><div class="goal-chips" aria-label="期望产物">${['验证一个判断','理解一个领域','复现一项结果','准备一篇论文'].map(goal=>`<button type="button" data-goal="${goal}" aria-pressed="${state.goal===goal}">${goal}</button>`).join('')}</div><p class="micro">默认：准备证据与计划 · 不提交外部实验 · 不发布</p><div class="action-row"><span class="micro">示例预算上限 US$5.00 <button type="button" class="text-button" data-action="budget">修改</button></span><button class="button primary" type="submit">开始梳理问题 →</button></div><p id="new-error" class="inline-error" hidden>请先描述一个问题，或从下方继续已有研究。</p></form><div class="section-heading"><h2>继续已有研究</h2><span>按需要你关注的事项排序</span></div><div class="output-row"><span class="output-icon">R01</span><div><h3>工具调用中的错误恢复 ${badge('待判断','amber')}</h3><p>需要确认等预算实验 · 上次停在协议 v3</p></div><button class="button" data-action="load-sample">继续研究 →</button></div><div class="note-box">本原型只有一个示例项目，不会创建真实研究记录。设计中的项目身份应独立于标题、语言和任务 ID。</div>`;
  }

  function defaultContext() {
    const review = needsRevalidation();
    
const initial = state.scenario === 'initial';
    return `<div class="context-heading"><h2>${initial?'当前计划':'需要你判断'}</h2>${badge(state.approved||state.deferred||initial?'0':'1')}</div>${initial?`<div class="attention-card"><span class="eyebrow">无需守候</span><h3>先准备证据，<br>再请你做重要决定。</h3><p>不会因为离开页面而丢弃已确认的研究材料。</p>${button('审阅研究范围','scope')}</div>`:review?`<div class="attention-card"><span class="eyebrow">依赖变化 · 需要复核</span><h3>先检查哪些判断受影响。</h3><p>保留旧版。来源范围或版本变化后，不能继续沿用旧实验依据。</p>${button('查看重新核验计划','recheck',true)}</div>`
:state.approved?`<div class="attention-card"><span class="eyebrow">确认已记录 · 演示</span><h3>实验已模拟排队。</h3><p>真实系统应保留操作 ID 与外部回执，即使工作区写入发生冲突。</p>${button('查看确认回执','receipt')}</div>`:state.deferred?`<div class="attention-card"><span class="eyebrow">本次已暂缓</span><h3>暂不提交实验。</h3><p>协议与已有证据仍然保留。实验不会自动启动。</p>${button('重新查看待判断事项','restore-decision')}</div>`:`<div class="attention-card"><span class="eyebrow">01 / 影响下一步的决定</span><h3>是否运行等预算<br>先导实验？</h3><p>区分结构化反馈的机制收益与额外计算收益。</p><div class="event-status">${badge('待批准','amber')}<span>上限 US$3.00</span></div>${button('审阅实验草案 →','approve',true)}<button class="rail-link" data-action="defer">暂不决定<span>↳</span></button></div>`}`
      + `<section class="context-section"><h3>研究范围</h3><dl class="context-dl"><dt>预期产物</dt><dd>${escape(state.goal)}</dd><dt>研究对象</dt><dd>${state.question?'尚待解析':'1–8B · 待确认'}</dd><dt>比较约束</dt><dd>${state.question?'尚待解析':'tokens + 工具上限'}</dd><dt>实验权限</dt><dd>${state.approved?'单次示例确认':'尚未授予'}</dd></dl><button class="rail-link" data-action="scope">查看假设与范围<span>↗</span></button></section><section class="context-section"><h3>研究操作预算 <span class="micro">示例</span></h3><div class="budget-label"><strong>US$${initial?'0.00':'1.20'}</strong><span>/ US$5.00 上限</span></div><div class="budget-line" aria-hidden="true"><span style="width:${initial?'0':'24'}%"></span></div><p>与“实验等推理预算”分开。<br>达到上限，保留结果并停止新增工作。</p></section><details class="context-section"><summary>最近有意义的变化</summary><ol class="timeline"><li>${initial?'已接收研究问题':'已保留预算混淆这一竞争解释'}<small>${initial?'等待第一份证据回执':'关联示例证据 B · 待复核'}</small></li><li>${initial?'正在准备来源范围':'实验草案已写入判断规则'}<small>${initial?'不显示虚构的阅读进度':'协议 v3 · 尚未运行'}</small></li></ol><button class="rail-link" data-action="activity">全部活动<span>↗</span></button></details><form class="context-question" id="question-form"><label for="context-question">问问这项研究</label><textarea class="input" id="context-question" maxlength="1000" placeholder="例如：什么结果会推翻这个判断？">${escape(state.drafts.question||'')}</textarea><button class="button full" type="submit">带着当前证据提问 ↗</button><div id="chat-response" role="status" aria-live="polite"></div></form>`;
  }
  function evidenceInspector(source) {
    return `<div class="context-heading"><h2 tabindex="-1" id="inspector-heading">证据详情 / ${source.id}</h2><button class="icon-button" data-action="close-inspector" aria-label="关闭证据详情">×</button></div><span class="source-icon">${source.id}</span><h2 class="inspector-title">${source.title}</h2><p class="micro">示例文献 · 非真实论文</p><p class="inspector-bibliography">${source.publicationYear ?? '年份未知'} · ${venueLabel(source)}<br>引用 ${source.citationCount === null ? '未知' : source.citationCount.toLocaleString('en-US')} · 合成快照</p><button class="text-button" data-metadata="${source.id}">查看会议与引用依据 ↗</button><div class="constraint-row">${badge(source.relation,source.type==='challenge'?'amber':source.type==='support'?'green':'gray')}${badge(source.coverage)}</div><div class="source-quote">${source.quote}</div><div class="source-location">${source.location}<br>${source.locator}</div><div class="action-row">${button(source.type==='unread'?'查看获取状态':'打开原文位置','source-page')}</div><div class="relation"><h3>这条证据能说明什么？</h3><p>${source.detail}</p></div><div class="relation"><h3>不能推导什么？</h3><p>${source.limitation}</p></div><div class="relation"><h3>核验与版本</h3><p>${source.basis}</p></div><button class="rail-link" data-action="trace">查看证据到判断的关联<span>→</span></button><p class="micro">引用存在 ≠ 语义支持 ≠ 科学结论已验证。</p>`;
  }
  function needsRevalidation() { return state.scopeDirty || state.scenario === 'stale'; }
  function needsDecision() { return !state.approved && !state.deferred && state.scenario !== 'initial'; }
  function announce(message) { $('#a11y-status').textContent = message; }
  function updateContextVisibility() {
    context.hidden = !state.contextOpen;
    $('.content-grid').classList.toggle('context-collapsed', !state.contextOpen);
    const control = $('.context-toggle');
    control.setAttribute('aria-expanded', String(state.contextOpen));
    control.textContent = state.contextOpen ? '收起上下文' : '显示上下文';
  }
  function unavailablePage() {
    return pageHeading('RESEARCH / 尚未形成这部分内容', '先准备依据，再形成判断。', '新问题的研究尚未产生来源、假设或实验结果。')
      + '<div class="empty-state"><h2>这不是等待动画，而是明确的边界。</h2><p>真实系统应在收到已确认的产物后开放内容。这里不会把另一个研究的文献或实验套用到新问题。</p><div class="action-row"><button class="button primary" data-page="brief">查看当前计划 →</button><button class="button" data-action="load-sample">查看另一份完整示例</button></div></div>';
  }
  
function render() {
    $('#project-name').textContent=state.question?'新研究草稿':'工具调用中的错误恢复';
    
main.innerHTML = state.initialDraft && !['home','brief'].includes(state.page) ? unavailablePage() : ({home:homePage,brief:briefPage,evidence:evidencePage,hypotheses:hypothesesPage,experiments:experimentsPage,outputs:outputsPage}[state.page])();
    if (state.page === 'evidence' && !state.initialDraft) {
      $('#relation-filter').value = state.relation;
      $('#venue-filter').value = state.venueFilter;
      $('#paper-sort').value = state.sort;
    }
    $('.content-grid').classList.toggle('evidence-layout', state.page === 'evidence');
    const selected = state.selected && sourceData.find(source=>source.id===state.selected);
    context.innerHTML = selected ? evidenceInspector(selected) : defaultContext();
    context.classList.toggle('is-inspecting',Boolean(selected));
    all('[data-page]').forEach(node=>{node.classList.toggle('active',node.dataset.page===state.page);if(node.closest('.tabs')){if(node.dataset.page===state.page)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');}});
    $('#scenario').value=state.scenario;
    all('.decision-count').forEach(node=>node.textContent=state.approved||state.deferred||state.scenario==='initial'?'0':'1');
    renderScenario();
    updateContextVisibility();
  }
  function renderScenario() {
    const messages = {
      disconnected:['连接已中断，不代表任务已停止。','以下显示最后确认的示例快照。重连不会重复提交任务。','模拟重新连接','reconnect',''],
      interrupted:['执行已中断，已确认的工作仍然保留。','继续时从检查点恢复；未完成的步骤可能重复。实验提交前应先核对远端状态。','查看恢复计划','resume',''],
      stale:['来源 B 的版本已变化，关联判断需要复核。','旧版简报保留。受影响的 H1 与实验依据，不再作为当前有效结论。','查看影响范围','stale-impact','error'],
      complete:['本轮交付已完成，不代表假设已验证。','可交付：证据比较与实验草案。没有真实实验结果，结论保持待验证。','查看研究产物','outputs',''],
      approval:['实验准备完成，等待你批准具体范围。','不会因为计划已生成而自动获得计算或发布权限。','审阅批准范围','approve','']
    };
    const message = state.scopeDirty
      ? ['来源选择已变化，当前仍显示旧版简报。','旧版使用 A–D 快照；新选择尚未重新核验，不能沿用旧实验批准。','查看重新核验计划','recheck','']
      : state.approved && state.scenario === 'approval'
        ? ['单次确认已模拟记录，任务已模拟排队。','没有真实提交，也没有实验结果。重复点击只查看同一回执。','查看回执','receipt','']
        : messages[state.scenario];
    $('#state-banner').innerHTML = message?`<div class="state-banner ${message[4]}"><div><strong>${message[0]}</strong><small>${message[1]}</small></div>${button(message[2],message[3])}</div>`:'';
    const status={initial:'准备来源范围 · 尚无语义阅读回执',synthesis:'整理证据 · 3 条节选 / 1 条元信息 · 示例状态',approval:'等待批准 · 实验尚未提交',disconnected:'连接中断 · 保留最后确认状态',interrupted:'已中断 · 检查点已保留',stale:'依赖变化 · 判断待复核',complete:'本轮已交付 · 科学结果仍未知'}[state.scenario];
    $('#run-status').innerHTML=`<span class="status-dot ${['stale','interrupted','disconnected','approval'].includes(state.scenario)?'amber':''}" aria-hidden="true"></span>${state.approved && state.scenario==='approval' ? '确认已模拟记录 · 模拟排队 · 科学结果未知' : status}`;
  }
  function navigate(page, focus=true) {
    if(!pages.includes(page))return;
    const draft=$('#context-question');if(draft)state.drafts.question=draft.value;
    if (page === 'evidence') state.contextOpen = false;
    else if (state.page === 'evidence') state.contextOpen = true;
    state.page=page;state.selected=null;location.hash=page+'?state='+state.scenario;render();
    if(focus){main.focus({preventScroll:true});window.scrollTo({top:0,behavior:'instant'});}
  }
  function setScenario(scenario) {
    if(!scenarios.includes(scenario))return;
    state.scenario=scenario;state.selected=null;

    state.approved=false;state.deferred=false;state.scopeDirty=false;state.sourceScope=new Set(['A','B','C','D']);
    state.initialDraft=scenario==='initial';state.question='';state.filter='';state.relation='all';

    state.evidenceView='literature';state.sort='relevance';state.venueFilter='all';state.topVenues=new Set(['ICLR','ICML','NeurIPS']);
    location.hash=state.page+'?state='+scenario;render();
  }
  function showDialog(title, body, footer='', kicker='研究工作台 / 交互演示') {
    if(!modal.open)dialogTrigger=document.activeElement;
    $('#modal-content').innerHTML=`<div class="modal-header"><div><span class="eyebrow">${kicker}</span><h2 id="modal-title" tabindex="-1">${title}</h2></div><button class="icon-button" data-action="close-modal" aria-label="关闭对话框">×</button></div><div class="modal-body">${body}</div>${footer?`<div class="modal-footer">${footer}</div>`:''}`;
    if(!modal.open)modal.showModal();

    $('#modal-title').focus({preventScroll:true});
  }
  function closeDialog(){modal.close();if(dialogTrigger?.isConnected)dialogTrigger.focus();}
  function notify(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').classList.add('visible');toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),4500);}
  function inspectSource(id, trigger) {
    const source=sourceData.find(row=>row.id===id);if(!source)return;
    state.selected=id;inspectorTrigger=trigger;

    state.contextOpen=true;updateContextVisibility();
    if(window.matchMedia('(max-width:1100px)').matches){showDialog('证据详情 / '+id,evidenceInspector(source));}
    else {context.innerHTML=evidenceInspector(source);context.classList.add('is-inspecting');$('#inspector-heading').focus({preventScroll:true});}
    all('#evidence-rows tr').forEach(row=>row.classList.toggle('selected',Boolean(row.querySelector(`[data-source="${id}"]`))));
  }
  function approveDialog() {
    if(state.initialDraft){showDialog('实验还未准备好','<p>当前只有问题与计划，没有可以批准的实验。</p>',button('关闭','close-modal'));return;}
    if(needsRevalidation()){showDialog('先重新核验实验依据','<p>来源版本已变化。保留协议 v3，但旧批准范围不能直接用于变化后的科学依据。请先查看影响范围。</p>',button('查看影响范围','stale-impact',true));return;}
    if(state.approved){action('receipt');return;}
    
showDialog('确认一次有边界的实验',`<p>不是“让 AI 自行决定”。你将批准以下固定范围。<strong>本原型不会调用任何真实工具。</strong></p><dl><div class="protocol-row"><dt>协议快照</dt><dd>EXP-01 · v3 · <code>demo-protocol-v3</code></dd></div><div class="protocol-row"><dt>执行对象</dt><dd>demo-eval / recovery-pilot · 24 条配对轨迹</dd></div><div class="protocol-row"><dt>动作与权限</dt><dd>单次 experiment.run；仅写示例结果目录。无发布权限，不删除或覆盖原数据。</dd></div><div class="protocol-row"><dt>预算与停止</dt><dd>最多 US$3.00 / 20 分钟；达到任一上限停止新增工作。</dd></div><div class="protocol-row"><dt>外部影响</dt><dd>真实实现将创建一个远端任务。停止不承诺回滚已发生的调用或费用。</dd></div><div class="protocol-row"><dt>范围变化</dt><dd>协议、工具签名、目标或上限变化，必须重新批准。</dd></div></dl><label class="approval-check"><input id="approval-check" type="checkbox">我已核对上述范围；理解这只是交互演示，不会运行真实实验。</label>`,button('返回修改','edit-protocol')+'<button class="button primary" id="approve-once" data-action="confirm-approval" disabled>模拟批准并排队 →</button>','APPROVAL / 一次性、限定范围');
  }
  function action(name) {

    if(name==='toggle-context'){state.contextOpen=!state.contextOpen;updateContextVisibility();return;}
    if(name==='load-sample'){state.initialDraft=false;state.question='';setScenario('synthesis');navigate('brief');return;}
    if(state.initialDraft && name==='activity'){showDialog('活动记录：只有已确认的事实','<ol class="plan-list"><li>研究问题已接收<small>未产生新的科学结论</small></li><li>等待第一份来源回执<small>没有已读论文、完成综述或运行结果</small></li></ol>',button('关闭','close-modal'));return;}
    if(state.scopeDirty && name==='brief-preview'){showDialog('旧版简报 · 待重新核验','<p>来源范围已修改，旧版结论仍基于 A–D 的演示快照。先核验受影响的判断，不能把旧内容标为新结果。</p>',button('查看核验计划','recheck',true));return;}
    if(name==='decisions' && !needsDecision()){
      showDialog('当前没有新的待批准事项', state.deferred ? '<p>等预算先导实验已暂缓。草案仍保留，不会自动执行。</p>' : state.initialDraft ? '<p>先准备证据与计划。还没有可以批准的实验。</p>' : '<p>示例确认已记录，实验已模拟排队；尚无结果。</p>',button('关闭','close-modal')+(state.deferred?button('恢复待判断事项','restore-decision'):''));return;
    }
    if(name==='decisions' && needsRevalidation()){showDialog('需要判断：来源变化的影响','<p>先重新核验关联判断，再决定是否更新实验依据。旧版仍可查阅。</p>',button('查看重新核验计划','recheck',true));return;}
    if(name==='commands'){showDialog('跳转到研究内容',`<div class="command-list">${[['home','研究首页'],['brief','研究简报'],['evidence','证据与文献'],['hypotheses','候选解释'],['experiments','实验与判断'],['outputs','研究产物']].map(([page,label],i)=>`<button data-command-page="${page}">${label}<kbd>${i+1}</kbd></button>`).join('')}</div>`);return;}
    if(name==='close-modal'){closeDialog();return;}
    if(name==='new'){navigate('home');setTimeout(()=>$('#new-question')?.focus(),0);return;}
    if(name==='outputs'){navigate('outputs');return;}
    if(name==='approve'){approveDialog();return;}
    if(name==='confirm-approval'){
      if(!$('#approval-check')?.checked)return;
      state.approved=true;closeDialog();navigate('experiments');notify('仅模拟：批准范围已记录，任务进入排队。没有实际提交，也没有实验结果。');return;
    }
    if(name==='receipt'){showDialog('确认回执 · 仅模拟',`<div class="note-box"><strong>操作 ID：</strong>demo-op-001<br><strong>状态：</strong>模拟排队 · 尚未执行<br><strong>科学结果：</strong>未知</div><p>真实实现必须以持久化操作记录去重，并关联远端任务 ID；工作区冲突不应丢失外部执行回执。</p>`,button('关闭','close-modal'));return;}
    if(name==='close-inspector'){if(modal.open)closeDialog();state.selected=null;context.innerHTML=defaultContext();context.classList.remove('is-inspecting');if(inspectorTrigger?.isConnected)inspectorTrigger.focus({preventScroll:true});return;}
    if(name==='source-page'){
      const s=sourceData.find(row=>row.id===state.selected)||sourceData[0];
      showDialog(s.type==='unread'?'尚未取得可核验原文':'原文位置 · 示意阅读器',s.type==='unread'?`<p>${s.quote} ${s.limitation}</p><div class="note-box">演示获取状态：全文不可用。真实产品应允许连接原文、补充文件或暂缓，不绕过访问控制。</div>`:`<div class="source-page"><span class="eyebrow">SYNTHETIC SOURCE / 非真实论文</span><h3>${s.title}</h3><p class="micro">${s.location}</p><p>本区展示来源上下文与精确定位的交互方式，不包含真实论文内容。</p><p><mark>${s.quote}</mark></p><p>范围说明：${s.limitation}</p></div><p class="source-location" style="margin-top:12px">${s.locator}</p>`,button('关闭原文预览','close-modal'));return;
    }
    if(name==='trace'){showDialog('从证据到判断，不丢失中间环节','<ol class="plan-list"><li>示例来源快照<small>固定版本与原文位置</small></li><li>范围受限的提取事实<small>与生成解释分开</small></li><li>H1 / H0 竞争解释<small>支持、限制与反证分别保留</small></li><li>协议 v3 → 运行证据 → 判断<small>后两项尚未产生，不绘制虚构的完成链</small></li></ol>',button('关闭','close-modal'));return;}
    if(name==='scope'||name==='edit-protocol'){
      showDialog(name==='scope'?'研究范围是可修正的假设':'修改协议前，先看影响',`<p>${name==='scope'?'系统可以准备默认范围，但不应把默认范围当作用户已经确认的科学事实。':'修改协议必须产生新版本。相关运行、判断和批准范围需要检查是否过期。'}</p><label for="scope-text" class="eyebrow" style="display:block;margin:15px 0 7px">补充或修改范围 · 仅保留本次演示草稿</label><textarea id="scope-text" class="input" rows="4" maxlength="2000">${escape(state.drafts.scope||'保持总生成 tokens 和工具调用上限相同；延迟单独报告。先导实验只决定下一步，不直接判定机制有效。')}</textarea><div class="note-box">真实产品需显示可审阅的差异，并通过 CAS 保存新版本。不得偷偷替换已批准的协议。</div>`,button('取消','close-modal')+button('保存演示草稿','save-scope',true));return;
    }
    if(name==='save-scope'){state.drafts.scope=$('#scope-text').value;closeDialog();notify('修改已保留为本次演示草稿；尚未改变协议快照或授权范围。');return;}
    if(name==='budget'){showDialog('两个预算，不能混为一谈','<dl><div class="protocol-row"><dt>研究操作预算</dt><dd>进行检索、分析和实验所消耗的费用、时间与计算资源。示例上限 US$5.00。</dd></div><div class="protocol-row"><dt>实验公平性</dt><dd>不同策略必须遵守的相同推理预算。不能因为研究经费充足而放宽某一组。</dd></div></dl><p>真实实现必须服务端强制限制，并保留授权与费用回执。此演示不修改实际配置。</p>',button('关闭','close-modal'));return;}
    if(name==='columns'){showDialog('比较字段必须有明确的提取规则','<dl><div class="protocol-row"><dt>年份与引用</dt><dd>年份优先正式发表年，只有预印本则标注首发年；引用数是指定数据库的带日期快照。数值排序，未知置底，同值按标题与来源 ID 稳定排序。</dd></div><div class="protocol-row"><dt>顶会录用</dt><dd>匹配具体会议、年份和 track 的官方录用或论文集记录；投稿页、标题和高引用都不构成录用证据。未发现记录不等于拒稿，Workshop 不等于主会。</dd></div><div class="protocol-row"><dt>预算可比性</dt><dd>原文是否明确匹配总生成 tokens、工具调用次数与延迟？分别记录，不将“报告成本”记为“已匹配”。</dd></div><div class="protocol-row"><dt>原文覆盖</dt><dd>标明全文、节选或仅元信息。本例无“全文已读”条目。</dd></div><div class="protocol-row"><dt>对判断的限制</dt><dd>仅从可定位原文提取限制；区分作者主张与研究助手的解释。</dd></div></dl><p>缺失时保留“未知”，不能用零或模型猜测补齐。成熟实现支持字段模板复用与每次提取的规则版本。</p>',button('关闭','close-modal'));return;}
    if(name==='venue-policy') {
      showDialog('顶会范围：明确规则，而不是质量评分', `<p>当前演示预设是 ICLR / ICML / NeurIPS 的主会；不是通用或官方排名。可按研究领域调整，Workshop 不自动计入。</p><fieldset class="venue-options"><legend>选择纳入顶会范围的会议 · 仅本次演示</legend>${venueOptions.map(venue=>`<label><input type="checkbox" data-venue-option="${venue}" ${state.topVenues.has(venue)?'checked':''}>${venue}</label>`).join('')}</fieldset><p>只有已核验录用且 track 为主会的论文才命中。范围变化只影响浏览标签与筛选，不改变来源选择或科学判断。</p>`,button('取消','close-modal')+button('应用演示范围','save-venue-policy',true));return;
    }
    if(name==='save-venue-policy') {
      state.topVenues=new Set(all('[data-venue-option]:checked',modal).map(node=>node.dataset.venueOption));
      closeDialog();render();$('[data-action="venue-policy"]').focus();announce('顶会范围已更新；来源选择与研究判断未改变。');return;
    }
    if(name==='clear-filter'){state.filter='';state.relation='all';state.venueFilter='all';render();$('#evidence-filter').focus();return;}
    if(name==='reset-scope'){state.sourceScope=new Set(['A','B','C','D']);state.scopeDirty=false;render();return;}
    if(name==='recheck'){showDialog('重新核验计划，不是重跑全部研究',`<p>新范围包含 ${state.sourceScope.size} 条示例来源。旧版简报保留，先检查受影响的提取事实、判断和假设。</p><ol class="plan-list"><li>复用未变化的来源快照</li><li>重新核验受影响的判断<small>不声称任务已经执行</small></li><li>展示新旧判断差异<small>没有回执前，不把旧版标为当前有效</small></li></ol>`,button('返回证据表','close-modal'));return;}
    if(name==='stale-demo'){setScenario('stale');return;}
    if(name==='stale-impact'){showDialog('只重新核验受影响的依赖','<div class="note-box"><strong>示例来源 B：v1 → v2</strong><br>受影响：预算可比性提取 → H1 / H0 的比较依据 → 协议 v3 的设计理由。</div><p>未受影响的来源 A、C 不必重新解析。旧版仍可查看；修订前的判断不继续标为有效。</p><div class="action-row">'+button('查看来源 B','inspect-B')+'</div>',button('关闭','close-modal'));return;}
    if(name==='inspect-B'){closeDialog();navigate('evidence');inspectSource('B',main);return;}
    if(name==='reconnect'){setScenario('synthesis');notify('已模拟重新取得快照；未重新提交任何任务。');return;}
    if(name==='resume'){showDialog('恢复前说明：会保留什么、重复什么','<dl><div class="protocol-row"><dt>已保留</dt><dd>示例文献记录、最后确认的阶段检查点和协议草案。</dd></div><div class="protocol-row"><dt>可能重复</dt><dd>尚未完成的综述步骤，复用已经保存的原文，不重做完整检索。</dd></div><div class="protocol-row"><dt>外部实验</dt><dd>先查询已有远端任务与操作回执；不能盲目重新提交。</dd></div></dl><p>这里只切换演示状态，不恢复任何真实服务端任务。</p>',button('暂不恢复','close-modal')+button('模拟从检查点继续','confirm-resume',true));return;}
    if(name==='confirm-resume'){closeDialog();setScenario('synthesis');notify('演示状态已恢复；已确认的科学内容未被改写。');return;}
    if(name==='activity'){showDialog('活动记录：保留事实，不抢走注意力','<ol class="plan-list"><li>研究问题已接收<small>建立示例项目，不以提问措辞作为身份</small></li><li>3 条节选已进入示例证据表<small>1 条仅元信息，全文覆盖未知</small></li><li>预算混淆已标为竞争解释<small>这是示例判断，不是实验结果</small></li><li>协议 v3 已准备<small>批准之前不执行</small></li></ol><p>真实产品只根据已确认的业务回执推进此记录。心跳和 token 流不等于科研进展。</p>',button('模拟中断状态','simulate-stop')+button('关闭','close-modal'));return;}
    if(name==='simulate-stop'){closeDialog();setScenario('interrupted');return;}
    if(name==='decisions'){showDialog('只保留需要你判断的事项',state.approved?'<p>没有待批准事项。示例实验已模拟排队；尚无结果。</p>':'<h3>等预算先导实验</h3><p>需要确认操作范围、预算上限与可证伪规则。普通检索完成通知不占用这个队列。</p>',button('稍后再看','close-modal')+(!state.approved?button('审阅批准范围','approve',true):''));return;}
    if(name==='defer'){state.deferred=true;context.innerHTML=defaultContext();all('.decision-count').forEach(node=>node.textContent='0');notify('本次演示中已暂缓；不会自动批准实验。');return;}
    if(name==='restore-decision'){state.deferred=false;if(modal.open)closeDialog();context.innerHTML=defaultContext();all('.decision-count').forEach(node=>node.textContent='1');return;}
    if(name.startsWith('hypothesis-')){showDialog('为什么保留这个竞争解释？',name.endsWith('H0')?'<p>如果收益可由相同预算的重试解释，就不能把结果归因于恢复机制。保留 H0 是为了让实验真正区分解释，而不是只验证偏好的方案。</p>':'<p>回滚针对的是状态污染，和结构化反馈的错误定位机制不同。但当前示例来源只有元信息，先补证据比立即提交实验更合理。</p>',button('关闭','close-modal'));return;}
    if(name==='readiness'){showDialog('就绪条件，不是一个好看的百分比','<ol class="plan-list"><li>问题与协议已明确<small>当前是演示草案</small></li><li>真实运行产物与回执<small>缺失：不能声称已支持假设</small></li><li>当前有效的证据与主张<small>来源、协议、运行依赖必须匹配</small></li><li>结果、限制和复现材料<small>不能用生成文本填补缺失实验</small></li><li>构建与发布分别确认<small>编译通过不等于科学有效</small></li></ol>',button('关闭','close-modal'));return;}
    if(name==='brief-preview'){showDialog('决策简报 · 示例草案','<span class="badge violet">待验证 · 非实验结论</span><h3 style="margin-top:15px">当前建议：先区分机制收益与预算收益。</h3><p>对比结构化错误反馈与等预算重试；保留 H0，先做配对先导。证据 A 支持定位机制，B 提醒预算混淆，C 定义级联指标。D 只有元信息，不能支持具体论证。</p><div class="note-box">下一步：审阅协议 v3。尚未批准外部实验，也不存在真实结果。</div>',button('关闭','close-modal'));return;}
    if(name==='about'){showDialog('一个科学工作台，不是任务监视器','<p>这个可交互设计研究融合结构化证据比较、上下文检查器、精确批准和可恢复状态。所有文献、预算、协议和回执均为示例，不连接 API，不持久化用户输入。</p><p style="margin-top:12px">完整设计原则、参考来源和实现边界见同目录 README.md。研究产物应有稳定身份和可追溯的科学依赖，而非仅依赖聊天记录。</p>',button('关闭','close-modal'));}
  }

  document.addEventListener('click',event=>{
    const node=event.target.closest('button,a');if(!node)return;
    if(node.dataset.page){event.preventDefault();navigate(node.dataset.page);}
    else if(node.dataset.commandPage){closeDialog();navigate(node.dataset.commandPage);}
    else if(node.dataset.evidenceView){state.evidenceView=node.dataset.evidenceView;render();$(`[data-evidence-view="${state.evidenceView}"]`).focus();announce(state.evidenceView === 'literature' ? '已切换到文献列表。' : '已切换到证据比较，保留相同来源与排序。');}
    else if(node.dataset.sortKey){const key=node.dataset.sortKey;state.sort=state.sort===key+'-desc'?key+'-asc':key+'-desc';refreshEvidence();$(`[data-sort-key="${key}"]`).focus();}
    else if(node.dataset.metadata){const source=sourceData.find(row=>row.id===node.dataset.metadata);if(source)showDialog('会议与引用依据 / '+source.id,metadataDetails(source),button('关闭','close-modal'),'BIBLIOGRAPHY / 合成示例');}
    else if(node.dataset.source){inspectSource(node.dataset.source,node);}
    else if(node.dataset.action){event.preventDefault();action(node.dataset.action);}
    else if(node.dataset.goal){state.goal=node.dataset.goal;all('[data-goal]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.goal===state.goal)));}
  });
  document.addEventListener('change',event=>{
    const node=event.target;
    if(node.id==='scenario'){state.approved=false;setScenario(node.value);}
    if(node.id==='approval-check')$('#approve-once').disabled=!node.checked;
    if(node.id==='relation-filter'){state.relation=node.value;refreshEvidence();}
    if(node.id==='venue-filter'){state.venueFilter=node.value;refreshEvidence();}
    if(node.id==='paper-sort'){state.sort=node.value;refreshEvidence();}
    if(node.dataset.scope){if(node.checked)state.sourceScope.add(node.dataset.scope);else state.sourceScope.delete(node.dataset.scope);state.scopeDirty=true;state.approved=false;$('#scope-count').textContent=state.sourceScope.size+' 条在当前选择中';$('#scope-draft').hidden=false;renderScenario();announce('已选择 '+state.sourceScope.size+' 条来源；旧版简报保留，关联判断待重新核验。');}
  });
  document.addEventListener('input',event=>{
    if(event.target.id==='evidence-filter'){state.filter=event.target.value;refreshEvidence();}
    if(event.target.id==='context-question')state.drafts.question=event.target.value;
  });
  document.addEventListener('submit',event=>{
    if(event.target.id==='new-form'){event.preventDefault();const input=$('#new-question');if(!input.value.trim()){$('#new-error').hidden=false;input.setAttribute('aria-invalid','true');input.setAttribute('aria-describedby','new-error');input.focus();return;}const entered=input.value.trim();setScenario('initial');state.question=entered;state.initialDraft=true;state.approved=false;state.deferred=false;navigate('brief');}
    if(event.target.id==='question-form'){event.preventDefault();const input=$('#context-question');if(!input.value.trim()){input.focus();notify('先写下你希望核查的问题。');return;}state.drafts.question=input.value;$('#chat-response').innerHTML='<div class="chat-demo"><strong>交互演示，不调用模型。</strong><br>真实回答应限定到当前来源快照，并保留引文。关键反证是：匹配预算后，结构化反馈与强重试基线没有可区分的差异。</div>';}
  });
  document.addEventListener('keydown',event=>{

    if(event.key==='Tab' && modal.open){
      const controls=all('button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),a[href],[tabindex="0"]',modal).filter(node=>node.getClientRects().length);
      const first=controls[0], last=controls[controls.length-1];
      if(controls.length && (!modal.contains(document.activeElement) || (event.shiftKey && document.activeElement.id==='modal-title') || (event.shiftKey ? document.activeElement===first : document.activeElement===last))){
        event.preventDefault();(event.shiftKey?last:first).focus();
      }
    }
    if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();action('commands');}
    if(event.key==='Escape'&&!modal.open&&state.selected){action('close-inspector');}
  });
  modal.addEventListener('close',()=>{if(dialogTrigger?.isConnected)dialogTrigger.focus({preventScroll:true});});
  modal.addEventListener('click',event=>{if(event.target===modal){const r=modal.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeDialog();}});
  function loadHash(){const [page,query]=location.hash.slice(1).split('?');if(pages.includes(page))state.page=page;const params=new URLSearchParams(query||'');if(scenarios.includes(params.get('state')))state.scenario=params.get('state');state.initialDraft=state.scenario==='initial';if(state.page==='evidence')state.contextOpen=false;render();}
  window.addEventListener('hashchange',()=>{const [page,query]=location.hash.slice(1).split('?');const scenario=new URLSearchParams(query||'').get('state');if(page!==state.page||(scenario&&scenario!==state.scenario))loadHash();});
  loadHash();
})();
