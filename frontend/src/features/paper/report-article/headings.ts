/** Stable article-local heading indexes and navigation; never scroll outer Paper chrome. */
import { escapeHtml } from '../../../html-safety';
import type { HeadingEntry } from './contracts';

function slug(text: string): string {
  return text.toLowerCase().trim().replace(/[^\w\u4e00-\u9fff\s-]/g, '')
    .replace(/\s+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || 'section';
}
export function indexHeadings(article: HTMLElement): HeadingEntry[] {
  const reserved = new Set([...article.querySelectorAll<HTMLElement>('[id]')].map(node => node.id));
  const claimed = new Set<string>();
  const entries: HeadingEntry[] = [];
  for (const heading of article.querySelectorAll<HTMLElement>('h2, h3')) {
    const text = heading.textContent?.trim();
    if (!text) continue;
    if (!heading.id || claimed.has(heading.id)) {
      const base = heading.id || 'report-' + slug(text);
      let id = base, suffix = 2;
      while (reserved.has(id)) id = base + '-' + suffix++;
      heading.id = id;
      reserved.add(id);
    }
    claimed.add(heading.id);
    entries.push({ id: heading.id, text, level: heading.tagName === 'H3' ? 3 : 2 });
  }
  return entries;
}
export function buildReportTableOfContents(entries: readonly HeadingEntry[], label: string): string {
  if (entries.length < 3) return '';
  const title = escapeHtml(label);
  return `<nav class="paper-report-toc" aria-label="${title}"><div class="paper-report-toc-title">${title}</div><ul>`
    + entries.map(entry => `<li class="toc-l${entry.level}"><a href="#${escapeHtml(entry.id)}" data-target="${escapeHtml(entry.id)}">${escapeHtml(entry.text)}</a></li>`).join('')
    + '</ul></nav>';
}
export function scrollReportElement(heading: HTMLElement, reducedMotion: boolean): void {
  const behavior: ScrollBehavior = reducedMotion ? 'auto' : 'smooth';
  const scroller = heading.closest<HTMLElement>('.paper-report-content, .paper-report-body');
  if (scroller) {
    const top = scroller.scrollTop + heading.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 16;
    scroller.scrollTo({ top: Math.max(0, top), behavior });
  } else heading.scrollIntoView({ behavior, block: 'start' });
  if (!heading.hasAttribute('tabindex')) heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
}
