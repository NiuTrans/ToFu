/** Report/review reader chrome. Owns bounded source-fenced DOM scopes only;
 * report-runtime owns tasks, and retained report.js owns canonical Markdown,
 * reading anchors, audit cards, notes and deepening; report-article owns
 * article decoration/navigation/interactions. No page numbers inferred.
 */
import { releaseReportArticle } from './report-article';
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createButton, createDropdown, createEmptyState } from '../../ui';
import { createLifecycleScope } from '../../lifecycle';

interface View { kind?: string; containerId?: string }
interface Ports {
  _activePaperId?: string; _paperHash?: string; _paperFileName?: string;
  _setPaperMobileView?: (view: string) => void;
  _paperAskQuestion?: (question: string) => void;
  _generatePaperReport?: () => void; _generatePaperReview?: () => void;
}
const ports = (): Ports => featureRegistry as unknown as Ports;
const scopes = new Map<HTMLElement, () => void>();
function release(host: HTMLElement): void { scopes.get(host)?.(); scopes.delete(host); }
export function destroyReportReaders(): void { for (const host of scopes.keys()) release(host); }
function node(tag: string, className: string, text = ''): HTMLElement {
  const element = document.createElement(tag); element.className = className; element.textContent = text; return element;
}
function shell(host: HTMLElement, view: View): { header: HTMLElement; current: () => boolean; cleanup: (fn: () => void) => void } {
  release(host);
  const scope = createLifecycleScope(); scopes.set(host, () => scope.destroy());
  const id = ports()._activePaperId || '', hash = ports()._paperHash || '';
  const current = (): boolean => !scope.signal.aborted && host.isConnected
    && (ports()._activePaperId || '') === id && (ports()._paperHash || '') === hash;
  const header = node('header', 'paper-document-header');
  header.append(node('p', 'paper-studio-eyebrow', t(view.kind === 'rebuttal' ? 'paper.rebuttalSectionTitle' : view.kind === 'review' ? 'paper.tabReview' : 'paper.tabReport')),
    node('h1', 'paper-document-source', ports()._paperFileName?.replace(/\.pdf$/i, '') || t('paper.documentSource')),
    node('p', 'paper-studio-provenance', t(view.kind === 'rebuttal' ? 'paper.documentRebuttalPurpose' : view.kind === 'review' ? 'paper.documentReviewPurpose' : 'paper.documentReportPurpose')));
  return { header, current, cleanup: fn => scope.add(fn) };
}
export function mountReportReader(host: HTMLElement, article: HTMLElement, view: View): void {
  const { header, current, cleanup } = shell(host, view);
  const controls = node('div', 'paper-document-controls');
  controls.append(createButton({ label: t('paper.translationOriginal'), variant: 'ghost', onClick: () => {
    if (current()) { ports()._setPaperMobileView?.('pdf'); document.getElementById('paperPdfViewer')?.focus({ preventScroll: true }); }
  } }));
  const headings = [...article.querySelectorAll<HTMLElement>('h2, h3')]
    .filter(heading => heading.id && heading.textContent?.trim()).slice(0, 32);
  if (headings.length) {
    const menu = createDropdown({ label: t('paper.reportTocLabel'), items: headings.map((heading, index) => ({
      id: String(index), label: heading.textContent!.trim(),
    })), onSelect: value => {
      if (!current()) return;
      const heading = headings[Number(value)]; if (!heading?.isConnected) return;
      const top = host.scrollTop + heading.getBoundingClientRect().top - host.getBoundingClientRect().top;
      host.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
      heading.tabIndex = -1; heading.focus({ preventScroll: true });
    } });
    controls.append(menu.el); cleanup(() => menu.close(false));
  }
  header.append(controls); host.prepend(header);
}
export function renderReportReady(host: HTMLElement, view: View, title: string, description: string, label: string): void {
  releaseReportArticle(host);
  const { header, current } = shell(host, view);
  const ready = createEmptyState({ title, description, action: { label, variant: 'primary', onClick: () => {
    if (current()) (view.kind === 'review' ? ports()._generatePaperReview : ports()._generatePaperReport)?.();
  } } });
  const main = node('section', 'paper-document-ready'); main.append(header, ready); host.replaceChildren(main);
}
