import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';
type JsonObject = Record<string, unknown>;

interface XPView extends JsonObject {
  kind?: string;
  containerId?: string;
  langKey?: () => string;
  meta?: unknown;
}

interface PaperReference {
  arxiv_id?: string;
  abs_url?: string;
  title?: string;
}

interface ConnectionItem {
  text?: string;
  paper?: PaperReference | null;
  anchor_idx?: number | null;
}

interface ProvocationItem {
  text?: string;
  anchor_idx?: number | null;
}

interface OpenProblemItem {
  text?: string;
  grounded_by?: PaperReference | null;
}

interface InsightItems {
  thesis?: string;
  opinion?: string;
  connections?: ConnectionItem[];
  provocations?: Array<ProvocationItem | string>;
  open_problems?: OpenProblemItem[];
}

interface CheckpointItem {
  anchor_idx?: number;
  question?: string;
  answer?: string;
}

interface InsightPayload {
  items?: InsightItems;
  markdown?: string;
}

interface CheckpointPayload {
  items?: CheckpointItem[];
}

interface SecondPassMeta {
  costCny?: number;
}

interface ReportMeta extends JsonObject {
  costCny?: number;
  secondPasses?: Record<string, SecondPassMeta | undefined>;
}

interface SessionStats {
  minutes?: number;
  words?: number;
}

type PaperXPWindow = Window & {
  _activePaperId?: string;
  _i18nLang?: string;
  t?: (key: string) => string;
  renderMarkdown?: (markdown: string) => string;
  formatCny?: (value: number) => string;
  showToast?: (message: string) => void;
  debugLog?: (message: string, level?: string) => void;
  _renderReportFinishTag?: (meta: unknown) => string;
  _paperAskQuestion?: (text: string) => void;
  _startResearchJob?: (text: string) => void;
  _paperDeepenAfterRender?: (
    article: Element,
    container: Element | null,
    view: XPView,
  ) => void;
  _paperNotesAfterRender?: (
    article: Element,
    container: Element | null,
    view: XPView,
  ) => void;
  _paperXpClickWired?: boolean;
  _paperXpSessionSummary?: typeof paperXpSessionSummary;
  _paperXpGet?: typeof paperXpGet;
  _paperXpSet?: typeof paperXpSet;
  _paperXpAfterRender?: typeof paperXpAfterRender;
  _paperXpHandleInsightEvent?: typeof paperXpHandleInsightEvent;
  _paperXpApplyMetaEvent?: typeof paperXpApplyMetaEvent;
  _paperXpDistribute?: typeof paperXpDistribute;
  _paperXpDistributeCheckpoints?: typeof paperXpDistributeCheckpoints;
  _paperXpHandleCheckpointsEvent?: typeof paperXpHandleCheckpointsEvent;
  _paperXpCostBreakdown?: typeof paperXpCostBreakdown;
  _destroyPaperXP?: typeof destroyPaperXP;
};

function globals(): PaperXPWindow {
  return featureRegistry as unknown as PaperXPWindow;
}

function translate(key: I18nKey, fallback: string = key): string {
  const helper = globals().t;
  return typeof helper === 'function' ? helper(key) : fallback;
}

const store: Record<string, JsonObject | undefined> = Object.create(null) as Record<
  string,
  JsonObject
>;

function xpKey(view: XPView | null | undefined): string {
  try {
    const state = globals();
    const lang = typeof view?.langKey === 'function' ? view.langKey() : '';
    return `${state._activePaperId || ''}::${view?.kind || ''}::${lang}`;
  } catch {
    return '';
  }
}

export function paperXpGet<T = unknown>(
  view: XPView | null | undefined,
  name: string,
): T | undefined {
  const saved = store[xpKey(view)];
  if (saved && saved[name] !== undefined) return saved[name] as T;
  return view?.[name] as T | undefined;
}

export function paperXpSet(
  view: XPView | null | undefined,
  name: string,
  value: unknown,
): void {
  const key = xpKey(view);
  if (key) {
    const saved = store[key] ??= {};
    saved[name] = value;
  }
  if (view) view[name] = value;
}

function refLink(reference: PaperReference | null | undefined): string {
  if (!reference?.arxiv_id) return '';
  const url = reference.abs_url || `https://arxiv.org/abs/${reference.arxiv_id}`;
  const title = reference.title || reference.arxiv_id;
  return ` (<a href="${escape(url)}" target="_blank" rel="noopener">`
    + `${escape(title)}</a>)`;
}

function card(className: string, icon: string, title: string, inner: string): string {
  return `<div class="paper-xp-card ${className}" role="note">`
    + `<div class="paper-xp-card-head">${icon}`
    + `<span class="paper-xp-card-title">${escape(title)}</span></div>`
    + `<div class="paper-xp-card-body">${inner}</div></div>`;
}

function connectionCard(item: ConnectionItem): string {
  const body = `<div class="paper-xp-conn">${escape(item.text || '')}`
    + `${refLink(item.paper)}</div>`;
  return card(
    'xp-conn', '🔗',
    translate('paper.xpConnTitle', 'Connections to your reading'),
    body,
  );
}

function actionButton(
  kind: 'debate' | 'ideate',
  text: string,
  key: I18nKey,
  fallback: string,
): string {
  return `<button type="button" class="paper-xp-act xp-act-${kind}"`
    + ` data-text="${escape(text)}">${escape(translate(key, fallback))}</button>`;
}

function provocationCard(item: ProvocationItem | string): string {
  const text = typeof item === 'string' ? item : item.text || '';
  return card(
    'xp-prov', '💭', translate('paper.xpProvTitle', 'Pause and think'),
    `<div class="paper-xp-prov">${escape(text)}</div>`
      + '<div class="paper-xp-actions">'
      + `${actionButton('debate', text, 'paper.xpDebate', 'Debate this')}</div>`,
  );
}

function openProblemCard(item: OpenProblemItem): string {
  const text = item.text || '';
  return card(
    'xp-open', '🧭', translate('paper.xpOpenTitle', 'Worth your Monday'),
    `<div class="paper-xp-open">${escape(text)}${refLink(item.grounded_by)}</div>`
      + '<div class="paper-xp-actions">'
      + `${actionButton('ideate', text, 'paper.xpIdeate', 'Turn into a proposal')}</div>`,
  );
}

function clearInsightNodes(article: Element): void {
  article.querySelectorAll(
    '.paper-xp-card, .paper-xp-section, .paper-xp-recap',
  ).forEach((node) => node.remove());
}

/** Distribute grounded insight cards around their resolved report headings. */
export function paperXpDistribute(article: Element, view: XPView): void {
  const payload = paperXpGet<InsightPayload>(view, '_xpInsight');
  const items = payload?.items;
  clearInsightNodes(article);
  if (!items) return;
  const chinese = globals()._i18nLang === 'zh';
  const headings = article.querySelectorAll<HTMLElement>('h2, h3');
  const connections = Array.isArray(items.connections) ? items.connections : [];
  const endConnections: ConnectionItem[] = [];
  connections.forEach((item) => {
    if (!item?.text?.trim()) return;
    const index = typeof item.anchor_idx === 'number' ? item.anchor_idx : null;
    if (index != null && headings[index]) {
      headings[index].insertAdjacentHTML('afterend', connectionCard(item));
    } else {
      endConnections.push(item);
    }
  });

  const provocations = Array.isArray(items.provocations) ? items.provocations : [];
  const endProvocations: string[] = [];
  provocations.forEach((item) => {
    const text = typeof item === 'string' ? item : item?.text || '';
    if (!text.trim()) return;
    const index = typeof item === 'object' && typeof item.anchor_idx === 'number'
      ? item.anchor_idx : null;
    if (index != null && headings[index]) {
      headings[index].insertAdjacentHTML('afterend', provocationCard(item));
    } else {
      endProvocations.push(text);
    }
  });

  const labels = chinese
    ? {
      section: '## 💡 洞见与灵感', thesis: '这篇论文的赌注',
      connections: '与你读过的工作的联系', opinion: '一个观点',
      open: '值得你周一动手的开放问题', provocations: '挑衅式追问',
    }
    : {
      section: '## 💡 Insight & Ideas', thesis: 'The Bet',
      connections: 'Connections to Your Reading', opinion: 'A Take',
      open: 'Open Problems Worth Your Monday', provocations: 'Provocations',
    };
  const thesis = (items.thesis || '').trim();
  const opinion = (items.opinion || '').trim();
  const openProblems = (Array.isArray(items.open_problems)
    ? items.open_problems : []).filter((item) => Boolean(item?.text?.trim()));
  const markdown: string[] = [];
  const hasEnd = Boolean(
    thesis || opinion || endConnections.length
    || openProblems.length || endProvocations.length,
  );
  if (hasEnd) {
    markdown.push(labels.section, '');
    if (thesis) {
      markdown.push(
        `### ${labels.thesis}`, '',
        `${chinese ? '> 关键结论：' : '> Key takeaway: '}${thesis}`, '',
      );
    }
    if (endConnections.length) {
      markdown.push(`### ${labels.connections}`, '');
      endConnections.forEach((item) => {
        const reference = item.paper;
        const suffix = reference?.arxiv_id
          ? ` ([${reference.title || reference.arxiv_id}](`
            + `${reference.abs_url || `https://arxiv.org/abs/${reference.arxiv_id}`}))`
          : '';
        markdown.push(`- ${(item.text || '').trim()}${suffix}`);
      });
      markdown.push('');
    }
    if (opinion) markdown.push(`### ${labels.opinion}`, '', opinion, '');
    const section = document.createElement('div');
    section.className = 'paper-xp-section';
    const render = globals().renderMarkdown;
    section.innerHTML = typeof render === 'function'
      ? render(markdown.join('\n')) : escape(markdown.join('\n'));
    if (openProblems.length) {
      const title = document.createElement('h3');
      title.textContent = labels.open;
      section.appendChild(title);
      openProblems.forEach((item) => {
        section.insertAdjacentHTML('beforeend', openProblemCard(item));
      });
    }
    if (endProvocations.length) {
      const title = document.createElement('h3');
      title.textContent = labels.provocations;
      section.appendChild(title);
      endProvocations.forEach((item) => {
        section.insertAdjacentHTML('beforeend', provocationCard(item));
      });
    }
    article.appendChild(section);
  }

  if (thesis || connections.length || openProblems.length) {
    const rows: string[] = [];
    if (thesis) {
      rows.push('<div class="paper-xp-recap-row"><b>'
        + `${escape(chinese ? '赌注' : 'The bet')}:</b> ${escape(thesis)}</div>`);
    }
    connections.slice(0, 2).forEach((item) => {
      rows.push(`<div class="paper-xp-recap-row">🔗 ${escape(item.text || '')}</div>`);
    });
    if (openProblems.length) {
      rows.push('<div class="paper-xp-recap-row">🧭 '
        + `${escape(openProblems[0].text || '')}</div>`);
    }
    const recap = document.createElement('div');
    recap.className = 'paper-xp-recap';
    recap.setAttribute('role', 'note');
    recap.innerHTML = '<div class="paper-xp-recap-head">📦 '
      + `${escape(translate('paper.xpRecapTitle', 'What you are taking away'))}</div>`
      + rows.join('');
    article.appendChild(recap);
  }
}

function checkpointCard(item: CheckpointItem): string {
  const title = translate('paper.xpCheckpointTitle', 'Checkpoint');
  const hint = translate('paper.xpFlipHint', 'tap to reveal the answer');
  return '<div class="paper-xp-flip" role="button" tabindex="0" '
    + `aria-label="${escape(item.question || '')}">`
    + '<div class="paper-xp-flip-face paper-xp-flip-front">'
    + '<div class="paper-xp-card-head">🧠'
    + `<span class="paper-xp-card-title">${escape(title)}</span>`
    + `<span class="paper-xp-flip-hint">${escape(hint)}</span></div>`
    + `<div class="paper-xp-flip-q">${escape(item.question || '')}</div></div>`
    + '<div class="paper-xp-flip-face paper-xp-flip-back">'
    + '<div class="paper-xp-card-head">✅'
    + `<span class="paper-xp-card-title">${escape(title)}</span></div>`
    + `<div class="paper-xp-flip-a">${escape(item.answer || '')}</div></div></div>`;
}

/** Insert checkpoint cards immediately before the next section heading. */
export function paperXpDistributeCheckpoints(article: Element, view: XPView): void {
  const payload = paperXpGet<CheckpointPayload>(view, '_xpCheckpoints');
  article.querySelectorAll('.paper-xp-flip').forEach((node) => node.remove());
  if (!Array.isArray(payload?.items) || !payload.items.length) return;
  const headings = article.querySelectorAll<HTMLElement>('h2, h3');
  payload.items.forEach((item) => {
    const index = item?.anchor_idx;
    if (typeof index !== 'number' || !headings[index]) return;
    let boundary = headings[index].nextElementSibling;
    while (boundary && !/^H[23]$/.test(boundary.tagName || '')) {
      boundary = boundary.nextElementSibling;
    }
    if (boundary) boundary.insertAdjacentHTML('beforebegin', checkpointCard(item));
    else article.insertAdjacentHTML('beforeend', checkpointCard(item));
  });
}

export function paperXpHandleCheckpointsEvent(
  stream: JsonObject,
  event: JsonObject,
  view: XPView,
): boolean {
  if (event.type !== 'checkpoints' || !Array.isArray(event.items)) return false;
  if (stream._checkpointsApplied) return true;
  stream._checkpointsApplied = true;
  const payload: CheckpointPayload = { items: event.items as CheckpointItem[] };
  stream._xpCheckpoints = payload;
  paperXpSet(view, '_xpCheckpoints', payload);
  try {
    const article = document.getElementById(view.containerId || '')
      ?.querySelector('.paper-report-article');
    if (article) paperXpDistributeCheckpoints(article, view);
  } catch (error: unknown) {
    console.warn('[Paper:XP] live checkpoint distribute failed (non-fatal):', error);
  }
  return true;
}

export function paperXpAfterRender(
  article: Element,
  container: Element | null,
  view: XPView,
): void {
  try {
    if (paperXpGet(view, '_xpInsight')) paperXpDistribute(article, view);
    if (paperXpGet(view, '_xpCheckpoints')) {
      paperXpDistributeCheckpoints(article, view);
    }
    globals()._paperDeepenAfterRender?.(article, container, view);
    globals()._paperNotesAfterRender?.(article, container, view);
  } catch (error: unknown) {
    console.warn('[Paper:XP] after-render distribute failed (non-fatal):', error);
  }
}

export function paperXpHandleInsightEvent(
  stream: JsonObject,
  event: JsonObject,
  view: XPView,
): boolean {
  if (!event.items || typeof event.items !== 'object') return false;
  stream._insightRunning = false;
  if (stream._insightApplied) return true;
  stream._insightApplied = true;
  const markdown = typeof event.insight === 'string' ? event.insight : '';
  stream.insightText = markdown;
  const payload: InsightPayload = {
    items: event.items as InsightItems,
    markdown,
  };
  stream._xpInsight = payload;
  paperXpSet(view, '_xpInsight', payload);
  try {
    const article = document.getElementById(view.containerId || '')
      ?.querySelector('.paper-report-article');
    if (article) paperXpDistribute(article, view);
  } catch (error: unknown) {
    console.warn('[Paper:XP] live distribute failed (non-fatal):', error);
  }
  return true;
}

export function paperXpApplyMetaEvent(
  stream: JsonObject,
  event: JsonObject,
  view: XPView,
): boolean {
  if (event.type !== 'report_meta' || !event.meta) return false;
  stream.meta = event.meta;
  view.meta = event.meta;
  try {
    const old = document.getElementById(view.containerId || '')
      ?.querySelector('.paper-report-finish-tag');
    const render = globals()._renderReportFinishTag;
    if (old && typeof render === 'function') {
      const html = render(event.meta);
      if (html) {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = html;
        if (wrapper.firstChild && old.parentNode) {
          old.parentNode.replaceChild(wrapper.firstChild, old);
        }
      }
    }
  } catch (error: unknown) {
    console.warn('[Paper:XP] report_meta finish-tag refresh failed (non-fatal):', error);
  }
  return true;
}

export function paperXpCostBreakdown(meta: ReportMeta | null | undefined): string {
  if (!meta?.secondPasses) return '';
  const format = (value: unknown): string | null => {
    if (typeof value !== 'number' || value <= 0) return null;
    return globals().formatCny?.(value) ?? `¥${value.toFixed(4)}`;
  };
  const parts: string[] = [];
  const base = format(meta.costCny);
  if (base) parts.push(`${translate('paper.xpCostBody')} ${base}`);
  const names: Record<string, I18nKey> = {
    insight: 'paper.xpPassInsight',
    termfill: 'paper.xpPassTermfill',
    checkpoints: 'paper.xpPassCheckpoints',
    deepen: 'paper.xpPassDeepen',
  };
  Object.entries(meta.secondPasses).forEach(([key, row]) => {
    const cost = format(row?.costCny);
    const labelKey = names[key];
    if (cost) parts.push(`${labelKey ? translate(labelKey) : key} ${cost}`);
  });
  return parts.join(' + ');
}

export function paperXpSessionSummary(
  stats: SessionStats | null | undefined,
  view: XPView,
): void {
  if (!stats?.minutes) return;
  const minutes = Math.max(1, Math.round(stats.minutes));
  const words = Math.max(0, Math.round(stats.words || 0));
  const notes = paperXpGet<unknown[]>(view, '_paperNotes');
  const noteCount = Array.isArray(notes) ? notes.length : 0;
  const message = globals()._i18nLang === 'zh'
    ? `本次阅读约 ${minutes} 分钟 · 覆盖约 ${words} 词${noteCount ? ` · ${noteCount} 条批注` : ''}`
    : `~${minutes} min read · ~${words} words covered${noteCount ? ` · ${noteCount} notes` : ''}`;
  const state = globals();
  if (typeof state.showToast === 'function') state.showToast(message);
  else state.debugLog?.(message, 'info');
}

const clickHandler = (event: Event): void => {
  const target = event.target as (Element & { closest?: Element['closest'] }) | null;
  const button = typeof target?.closest === 'function'
    ? target.closest<HTMLElement>('.paper-xp-act') : null;
  const flip = typeof target?.closest === 'function'
    ? target.closest<HTMLElement>('.paper-xp-flip') : null;
  if (button) {
    const text = button.getAttribute('data-text') || '';
    if (button.classList.contains('xp-act-debate')) {
      globals()._paperAskQuestion?.(text);
    } else if (button.classList.contains('xp-act-ideate')) {
      globals()._startResearchJob?.(text);
    }
    return;
  }
  flip?.classList.toggle('is-flipped');
};

export function destroyPaperXP(): void {
  document.removeEventListener('click', clickHandler);
  globals()._paperXpClickWired = false;
}

export function installPaperXPGlobals(): void {
  const target = globals();
  target._paperXpSessionSummary = paperXpSessionSummary;
  target._paperXpGet = paperXpGet;
  target._paperXpSet = paperXpSet;
  target._paperXpAfterRender = paperXpAfterRender;
  target._paperXpHandleInsightEvent = paperXpHandleInsightEvent;
  target._paperXpApplyMetaEvent = paperXpApplyMetaEvent;
  target._paperXpDistribute = paperXpDistribute;
  target._paperXpDistributeCheckpoints = paperXpDistributeCheckpoints;
  target._paperXpHandleCheckpointsEvent = paperXpHandleCheckpointsEvent;
  target._paperXpCostBreakdown = paperXpCostBreakdown;
  target._destroyPaperXP = destroyPaperXP;
  if (!target._paperXpClickWired) {
    target._paperXpClickWired = true;
    document.addEventListener('click', clickHandler);
  }
}

installPaperXPGlobals();
