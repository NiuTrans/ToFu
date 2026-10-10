/** Paper reading workbench: source identity, grounded report excerpts and Studio
 * entry points. Owns only DOM; report/media state and paid work stay with their
 * existing owners. Session supplies navigation and the read-only report loader.
 */
import { scrollReportElement } from './report-article/headings';
import { createButton, createTabs, type TabsHandle } from '../../ui';
import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';

type State = Record<string, any>;
const state = (): State => featureRegistry as unknown as State;
let tabs: TabsHandle | null = null;
let syncing = false;
let overview: HTMLElement | null = null;
let generation = 0;

function tr(key: I18nKey): string { return String(state().t?.(key) || key); }
function text(tag: string, className: string, value: string): HTMLElement {
  const node = document.createElement(tag); node.className = className;
  node.textContent = value; return node;
}
function open(tab: string): void {
  state()._switchPaperTab?.(tab); state()._setPaperMobileView?.('reader');
}
function action(key: I18nKey, tab: string): HTMLButtonElement {
  return createButton({ label: tr(key), variant: 'secondary',
    onClick: () => open(tab) });
}

/** Compose the retained panels through the kit's keyboard-accessible tab owner. */
export function mountPaperWorkbench(): void {
  if (tabs?.el.isConnected) return;
  const legacy = document.querySelector<HTMLElement>('#paperModeContainer .paper-tabs');
  if (!legacy) return;
  const panels = Array.from(document.querySelectorAll<HTMLElement>('#paperModeContainer .paper-tab-panel'));
  overview = document.createElement('section'); overview.className = 'paper-tab-panel paper-workbench';
  overview.dataset.tab = 'overview';
  const names: Record<string, I18nKey> = { overview: 'paper.workbenchTab', qa: 'paper.tabQA',
    report: 'paper.tabReport', review: 'paper.tabReview', translate: 'paper.tabBabel',
    podcast: 'paper.tabPodcast', video: 'paper.tabVideo' };
  let ready = false;
  tabs = createTabs({ tabs: [overview, ...panels].map(panel => ({
    id: panel.dataset.tab!, label: tr(names[panel.dataset.tab!]), panel,
  })), activeId: 'overview', onChange: tab => {
    if (ready && !syncing) open(tab);
  } });
  tabs.el.classList.add('paper-workbench-tabs');
  tabs.el.firstElementChild?.classList.add('paper-workbench-navigation');
  tabs.el.querySelectorAll<HTMLElement>('[role="tab"]').forEach((button, index) => {
    button.dataset.paperTab = ['overview', ...panels.map(panel => panel.dataset.tab!)][index];
    button.dataset.i18n = names[button.dataset.paperTab];
  });
  legacy.replaceWith(tabs.el); ready = true;
}

export function syncPaperWorkbench(tab: string): void {
  if (!tabs) return;
  syncing = true;
  try { tabs.activate(tab); } finally { syncing = false; }
}

function mediaStatus(tab: 'podcast' | 'video', hash: string): string {
  const media = state()[tab === 'podcast' ? '_podcast' : '_pvideo'];
  if (!hash || media?.paperHash !== hash) return tr('paper.workbenchConfigure');
  const keys: Record<string, I18nKey> = {
    done: 'paper.workbenchReady', generating: 'paper.workbenchRunning',
    loading: 'paper.workbenchChecking', interrupted: 'paper.workbenchInterrupted',
    aborted: 'paper.workbenchInterrupted', error: 'paper.workbenchRetry',
    lost: 'paper.workbenchRetry', lookup_failed: 'paper.workbenchRetry',
    report_required: 'paper.workbenchNeedsReport',
  };
  return tr(keys[media.status] || 'paper.workbenchConfigure');
}

/** Refresh only on entry. No extra polling, media lookup, or durable client cache. */
export function renderPaperWorkbench(failed = false): void {
  if (!overview) return;
  const shared = state(); const view = shared._reportView?.('report');
  const hasSource = Boolean(shared._paperHash || shared._paperParsedText || shared._paperPdfUrl);
  const fragment = document.createDocumentFragment();
  const header = text('header', 'paper-workbench-header', '');
  header.append(text('p', 'paper-workbench-eyebrow', tr('paper.workbenchEyebrow')),
    text('h1', 'paper-workbench-title', shared._paperFileName || tr('paper.workbenchNoSource')));
  const provenance = [shared._paperArxivId ? `arXiv ${shared._paperArxivId}` : '',
    shared._paperTotalPages ? `${shared._paperTotalPages} ${tr('paper.workbenchPages')}` : ''].filter(Boolean);
  header.append(text('p', 'paper-workbench-provenance', provenance.join(' · ') || tr('paper.workbenchSourceHint')));
  fragment.append(header);

  const brief = text('section', 'paper-workbench-brief', '');
  const insight = shared._paperXpGet?.(view, '_xpInsight') || view?._xpInsight;
  const thesis = typeof insight?.items?.thesis === 'string' ? insight.items.thesis.trim() : '';
  const report = document.getElementById(String(view?.containerId || 'paperReportContent'));
  // A settled canonical report is the only source of brief/outline claims.
  const settled = Boolean(view?.cache && view?.stream?.status !== 'running');
  const article = settled ? report?.querySelector('.paper-report-article') : null;
  const firstParagraph = article?.querySelector('p')?.textContent?.trim() || '';
  brief.append(text('p', 'paper-workbench-eyebrow', tr('paper.workbenchStartHere')),
    text('h2', 'paper-workbench-section-title', tr(settled ? 'paper.workbenchTakeaway' : 'paper.workbenchReadFirst')),
    text('p', 'paper-workbench-abstract', settled && (thesis || firstParagraph)
      ? (thesis || firstParagraph).slice(0, 900)
      : tr(failed ? 'paper.workbenchLookupFailed' : 'paper.workbenchReadHint')));
  const controls = text('div', 'ui-row ui-row--wrap', '');
  controls.append(action('paper.workbenchReadReport', 'report'), action('paper.workbenchAsk', 'qa'));
  for (const button of controls.querySelectorAll('button')) button.disabled = !hasSource;
  brief.append(controls); fragment.append(brief);

  const roadmap = text('section', 'paper-workbench-roadmap', '');
  roadmap.append(text('h2', 'paper-workbench-section-title', tr('paper.workbenchReadingPath')));
  const headings = Array.from(article?.querySelectorAll<HTMLElement>(':scope > h2, :scope > h3') || []).slice(0, 6);
  if (headings.length) {
    const list = document.createElement('ol'); list.className = 'paper-workbench-outline';
    headings.forEach((heading, index) => {
      const row = document.createElement('li');
      row.append(text('span', 'paper-workbench-number', String(index + 1).padStart(2, '0')));
      const body = text('div', 'paper-workbench-outline-body', '');
      body.append(text('h3', 'paper-workbench-outline-title', heading.textContent?.trim() || tr('paper.workbenchReadReport')));
      let next = heading.nextElementSibling;
      while (next && !next.matches('p,h2,h3')) next = next.nextElementSibling;
      if (next?.matches('p')) body.append(text('p', 'paper-workbench-excerpt', (next.textContent || '').slice(0, 170)));
      body.append(createButton({ label: tr('paper.workbenchReadSection'), size: 'sm',
        variant: 'ghost', onClick: () => {
          open('report');
          // The report owner can repaint on tab entry. Resolve the new heading.
          const next = report?.querySelector('.paper-report-article')?.querySelectorAll<HTMLElement>(':scope > h2, :scope > h3')[index];
          if (next) scrollReportElement(next, Boolean(shared.prefersReducedMotion?.()));
        } }));
      row.append(body); list.append(row);
    }); roadmap.append(list);
  } else {
    const prompts: I18nKey[] = ['paper.workbenchQuestionProblem', 'paper.workbenchQuestionMethod',
      'paper.workbenchQuestionEvidence', 'paper.workbenchQuestionLimits'];
    const list = text('div', 'paper-workbench-questions', '');
    prompts.forEach((key, index) => {
      const row = text('div', 'paper-workbench-question', '');
      const body = text('div', 'paper-workbench-outline-body', '');
      body.append(text('p', 'paper-workbench-outline-title', tr(key)),
        createButton({ label: tr('paper.workbenchAskQuestion'), variant: 'ghost', size: 'sm', disabled: !hasSource,
          onClick: () => { shared._paperAskQuestion?.(tr(key)); } }));
      row.append(text('span', 'paper-workbench-number', String(index + 1).padStart(2, '0')), body);
      list.append(row);
    }); roadmap.append(list);
  }
  fragment.append(roadmap);

  const studio = text('section', 'paper-workbench-studio', '');
  const studioHead = text('div', 'paper-workbench-studio-heading', '');
  studioHead.append(text('h2', 'paper-workbench-section-title', tr('paper.workbenchStudio')),
    text('p', 'paper-workbench-excerpt', tr('paper.workbenchStudioHint'))); studio.append(studioHead);
  const grid = text('div', 'paper-workbench-studio-grid', '');
  const modules: Array<[string, I18nKey, I18nKey]> = [
    ['podcast', 'paper.tabPodcast', 'paper.workbenchPodcastHint'],
    ['video', 'paper.tabVideo', 'paper.workbenchVideoHint'],
    ['translate', 'paper.tabBabel', 'paper.workbenchTranslateHint'],
    ['review', 'paper.tabReview', 'paper.workbenchReviewHint'],
  ];
  modules.forEach(([tab, title, hint]) => {
    const card = text('article', `paper-workbench-tool paper-workbench-tool-${tab}`, '');
    card.append(text('span', 'paper-workbench-tool-mark', tab === 'podcast' ? '◖◗' : tab === 'video' ? '▷' : tab === 'translate' ? 'A文' : '✓'),
      text('h3', 'paper-workbench-tool-title', tr(title)), text('p', 'paper-workbench-excerpt', tr(hint)));
    if (tab === 'podcast' || tab === 'video') card.append(text('p', 'paper-workbench-tool-status', mediaStatus(tab, String(shared._paperHash || ''))));
    card.append(createButton({ label: tr('paper.workbenchOpen'), size: 'sm', disabled: !hasSource,
      onClick: () => open(tab) })); grid.append(card);
  }); studio.append(grid); fragment.append(studio);
  overview.replaceChildren(fragment);
}

export async function openPaperWorkbench(load: () => Promise<void>): Promise<void> {
  const token = ++generation;
  const paper = String(state()._activePaperId || '');
  renderPaperWorkbench();
  if (!state()._paperHash && !state()._paperParsedText) return;
  let failed = false;
  try { await load(); } catch { failed = true; }
  if (token === generation && state().paperMode && state()._paperActiveTab === 'overview'
      && paper === String(state()._activePaperId || '')) renderPaperWorkbench(failed);
}

export function leavePaperWorkbench(): void { generation += 1; }
