/** Mounted article interactions and scroll-spy. Repaint/ready/exit release each host scope. */
import { createLifecycleScope } from '../../../lifecycle';
import { positionGlossaryCard } from './figures';
import { scrollReportElement } from './headings';
import type { ArticlePorts } from './contracts';

const mounted = new Map<HTMLElement, () => void>();
export function releaseReportArticle(host: HTMLElement): void {
  const release = mounted.get(host);
  mounted.delete(host);
  release?.();
}
export function destroyReportArticles(): void {
  for (const host of mounted.keys()) releaseReportArticle(host);
}
export function mountReportArticle(host: HTMLElement, article: HTMLElement, ports: ArticlePorts): void {
  releaseReportArticle(host);
  const scope = createLifecycleScope();
  mounted.set(host, () => scope.destroy());
  const current = (): boolean => !scope.signal.aborted && host.isConnected && host.contains(article);
  const target = (event: Event): Element | null => event.target instanceof Element ? event.target : null;
  const position = (event: Event): void => {
    const term = target(event)?.closest<HTMLElement>('.paper-term');
    if (current() && term && article.contains(term)) positionGlossaryCard(term);
  };
  scope.listen(article, 'mouseover', position);
  scope.listen(article, 'focusin', position);
  scope.listen(host, 'click', event => {
    if (!current()) return;
    const image = target(event)?.closest<HTMLImageElement>('img');
    if (image && article.contains(image)) { ports.openImage(image.src); return; }
    const link = target(event)?.closest<HTMLAnchorElement>('.paper-report-toc a[data-target]');
    if (!link || !host.contains(link)) return;
    const heading = [...article.querySelectorAll<HTMLElement>('h2, h3')].find(node => node.id === link.dataset.target);
    if (!heading) return;
    event.preventDefault();
    scrollReportElement(heading, ports.reducedMotion());
  });
  scope.listen(article, 'keydown', event => {
    const key = event as KeyboardEvent;
    if (!current() || !['Enter', ' ', 'Spacebar'].includes(key.key)) return;
    const image = target(event)?.closest<HTMLImageElement>('img');
    if (!image || !article.contains(image)) return;
    key.preventDefault(); ports.openImage(image.src);
  });
  const toc = host.querySelector<HTMLElement>('.paper-report-toc');
  const headings = [...article.querySelectorAll<HTMLElement>('h2, h3')];
  const Observer = host.ownerDocument.defaultView?.IntersectionObserver;
  if (!toc || !headings.length || !Observer) return;
  const links = new Map([...toc.querySelectorAll<HTMLAnchorElement>('a[data-target]')].map(link => [link.dataset.target, link]));
  const visible = new Set<Element>();
  try {
    const observer = new Observer(items => {
      if (!current()) return;
      for (const item of items) { if (item.isIntersecting) visible.add(item.target); else visible.delete(item.target); }
      const first = headings.find(heading => visible.has(heading));
      for (const [id, link] of links) {
        const active = id === first?.id;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current');
      }
    }, { root: host, rootMargin: '0px 0px -70% 0px', threshold: 0 });
    scope.add(() => observer.disconnect());
    for (const heading of headings) observer.observe(heading);
  } catch (error) {
    releaseReportArticle(host);
    throw error;
  }
}
