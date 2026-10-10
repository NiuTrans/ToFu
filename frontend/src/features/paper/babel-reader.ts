/** Translation artifact reader: source identity, language, chapters and Q&A.
 * Owns one DOM/listener/export scope. babel.ts owns requests and cancellation;
 * rendered headings come from the artifact, without estimated page citations.
 */
import { featureRegistry } from '../../feature-registry';
import { t, _i18nLang } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { escapeHtml } from '../../html-safety';
import { createButton, createDropdown, createAlert, createProgress, createEmptyState } from '../../ui';

export interface TranslationView {
  lang: string; phase: 'idle' | 'loading' | 'running' | 'done' | 'error' | 'stopped';
  text: string; done: number; total: number; failure?: string; lookupFailed?: boolean; stopUnconfirmed?: boolean;
}
interface Commands { select: (lang: string) => void; generate: () => void; stop: () => void }
interface Ports {
  _activePaperId?: string; _paperHash?: string; _paperFileName?: string; _paperParsedText?: string;
  renderMarkdown?: (text: string) => string; _setPaperMobileView?: (view: string) => void;
  _paperAskQuestion?: (question: string) => void;
}
const ports = (): Ports => featureRegistry as unknown as Ports;
const languages: Record<string, string> = { zh: '中文', en: 'English', ja: '日本語' };
function node<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text = ''): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag); element.className = className; element.textContent = text; return element;
}
let mounted: { host: HTMLElement; key: string; scope: LifecycleScope; refresh: (view: TranslationView) => void } | null = null;
export function destroyTranslationReader(): void { mounted?.scope.destroy(); mounted = null; }

export function renderTranslationReader(host: HTMLElement, view: TranslationView, commands: Commands): void {
  const paperId = ports()._activePaperId || '', hash = ports()._paperHash || '';
  const key = JSON.stringify([paperId, hash, _i18nLang]);
  if (mounted?.host === host && mounted.key === key && host.isConnected) { mounted.refresh(view); return; }
  const scrollTop = host.scrollTop; destroyTranslationReader();
  const scope = createLifecycleScope();
  const current = (): boolean => !scope.signal.aborted && (ports()._activePaperId || '') === paperId && (ports()._paperHash || '') === hash;
  let latest = view;
  const main = node('article', 'paper-studio-reader paper-translation-reader');
  const header = node('header', 'paper-studio-header');
  const title = ports()._paperFileName?.replace(/\.pdf$/i, '') || t('paper.translationTitle');
  header.append(node('p', 'paper-studio-eyebrow', t('paper.translationTitle')),
    node('h1', 'paper-studio-title', title), node('p', 'paper-studio-provenance', t('paper.translationSource')));
  main.append(header);
  const tools = node('div', 'paper-studio-controls');
  const language = createDropdown({ label: t('paper.translationChoose'),
    items: Object.entries(languages).map(([id, label]) => ({ id, label })),
    onSelect: lang => { if (current()) commands.select(lang); } });
  const generate = createButton({ label: t('paper.translationGenerate'), variant: 'primary', onClick: () => { if (current()) commands.generate(); } });
  const stop = createButton({ label: t('paper.translationStop'), onClick: () => { if (current()) commands.stop(); } });
  const source = createButton({ label: t('paper.translationOriginal'), variant: 'ghost', onClick: () => {
    if (current()) { ports()._setPaperMobileView?.('pdf'); document.getElementById('paperPdfViewer')?.focus({ preventScroll: true }); }
  } });
  tools.append(language.el, generate, stop, source); main.append(tools);
  scope.add(() => language.close(false));
  const status = node('div', 'paper-translation-status'); status.id = 'babelPdfStatus'; status.setAttribute('role', 'status');
  main.append(status);
  const feedback = node('p', 'paper-studio-feedback'); feedback.setAttribute('role', 'status'); main.append(feedback);
  const roadmap = node('nav', 'paper-translation-roadmap'); roadmap.setAttribute('aria-label', t('paper.translationChapters')); main.append(roadmap);
  const body = node('div', 'paper-translation-body'); body.id = 'babelPdfBody'; main.append(body);
  const footer = node('footer', 'paper-studio-controls');
  let exportUrl = '', exportTimer = 0;
  const releaseExport = (): void => { if (exportTimer) window.clearTimeout(exportTimer); exportTimer = 0;
    if (exportUrl) URL.revokeObjectURL(exportUrl); exportUrl = ''; };
  scope.add(releaseExport);
  const copy = createButton({ label: t('paper.translationCopy'), onClick: () => {
    if (!current() || !latest.text) return;
    const text = latest.text;
    if (!navigator.clipboard?.writeText) { feedback.textContent = t('paper.translationCopyFailed'); return; }
    void navigator.clipboard.writeText(text).then(() => {
      if (current() && latest.text === text) feedback.textContent = t('paper.translationCopied');
    }).catch(() => { if (current() && latest.text === text) feedback.textContent = t('paper.translationCopyFailed'); });
  } });
  const download = createButton({ label: t('paper.translationDownload'), onClick: () => {
    if (!current() || !latest.text) return;
    releaseExport(); exportUrl = URL.createObjectURL(new Blob([latest.text], { type: 'text/markdown;charset=utf-8' }));
    const anchor = document.createElement('a'); anchor.href = exportUrl;
    anchor.download = `paper-translation-${hash.slice(0, 8) || 'source'}-${latest.lang}.md`; anchor.click();
    exportTimer = window.setTimeout(releaseExport, 1000);
  } });
  footer.append(copy, download); main.append(footer);
  let contentKey = '', contentScope: LifecycleScope | null = null;
  scope.add(() => contentScope?.destroy());
  const refresh = (next: TranslationView): void => {
    if (!current()) return;
    latest = next;
    const trigger = language.el.querySelector('button');
    if (trigger) trigger.textContent = next.lang ? t('paper.translationLanguage', { lang: languages[next.lang] || next.lang }) : t('paper.translationChoose');
    const running = next.phase === 'running';
    generate.disabled = running || !next.lang || !ports()._paperParsedText || !!next.text;
    generate.hidden = !!next.text; stop.hidden = !running;
    footer.hidden = !next.text; body.hidden = !next.text && (running || next.phase === 'loading'); status.replaceChildren();
    if (next.phase === 'running' || next.phase === 'loading') {
      const message = next.phase === 'loading' ? t('paper.translationLoading')
        : next.total > 0 ? t('paper.babelTranslatedCount', { done: next.done, total: next.total })
          : t('paper.babelTranslatingTo', { lang: languages[next.lang] || next.lang });
      status.append(node('p', 'paper-studio-note', message));
      status.append(createProgress({ value: next.total > 0 ? next.done / next.total * 100 : undefined, label: message }).el);
    } else if (next.phase === 'error') {
      status.append(createAlert({ variant: 'warning', title: t('paper.babelFailed'), body: next.failure || undefined }).el);
    } else if (next.phase === 'stopped') {
      status.append(createAlert({ variant: next.stopUnconfirmed ? 'warning' : 'info',
        title: t(next.stopUnconfirmed ? 'paper.translationStopUnconfirmed' : 'paper.translationStopped') }).el);
    } else if (next.lookupFailed) status.append(createAlert({ variant: 'warning', title: t('paper.translationLookupFailed') }).el);
    status.hidden = !status.childElementCount;
    // A terminal artifact is rendered once. Progress never rebuilds its DOM.
    const signature = JSON.stringify([next.lang, next.text]);
    if (signature === contentKey) return;
    contentKey = signature; contentScope?.destroy(); contentScope = createLifecycleScope();
    const children = contentScope;
    body.replaceChildren(); roadmap.replaceChildren(); feedback.textContent = '';
    if (!next.text) {
      roadmap.hidden = true;
      body.append(createEmptyState({ title: t('paper.translationReady'), description: t('paper.translationReadyHint') })); return;
    }
    const article = node('article', 'paper-report-article paper-translation-article');
    article.innerHTML = ports().renderMarkdown?.(next.text) || `<pre class="paper-translation-plain">${escapeHtml(next.text)}</pre>`;
    body.append(article);
    const headings = [...article.querySelectorAll<HTMLElement>('h2,h3')].slice(0, 32);
    roadmap.hidden = !headings.length;
    if (headings.length) {
      roadmap.append(node('h2', 'paper-studio-section-title', t('paper.translationChapters')));
      const list = node('ol', 'paper-translation-chapters');
      headings.forEach((heading, index) => {
        const label = heading.textContent?.trim().slice(0, 160) || t('paper.studioSceneTitle', { index: index + 1 });
        const row = node('li', '');
        row.append(createButton({ label, variant: 'ghost', size: 'sm', onClick: () => {
          if (!current() || children.signal.aborted) return;
          // Scroll only the reader host; navigation remains in its fixed place.
          host.scrollTop += heading.getBoundingClientRect().top - host.getBoundingClientRect().top;
          heading.tabIndex = -1; heading.focus({ preventScroll: true });
        } })); list.append(row);
        const paragraph = heading.nextElementSibling;
        const context = `${label}\n${paragraph?.matches('p') ? paragraph.textContent || '' : ''}`.slice(0, 600);
        const actions = node('div', 'paper-translation-section-actions');
        actions.append(createButton({ label: t('paper.studioAsk'), variant: 'ghost', size: 'sm', onClick: () => {
          if (current() && !children.signal.aborted) ports()._paperAskQuestion?.(t('paper.translationAskPassage', { passage: context }));
        } })); (paragraph?.matches('p') ? paragraph : heading).insertAdjacentElement('afterend', actions);
      }); roadmap.append(list);
    }
  };
  mounted = { host, key, scope, refresh }; host.replaceChildren(main); scope.add(() => main.remove()); refresh(view); host.scrollTop = scrollTop;
  scope.listen(window, 'tofu:language-change', () => { if (current()) renderTranslationReader(host, latest, commands); });
}
