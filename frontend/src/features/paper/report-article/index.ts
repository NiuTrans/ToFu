/** Final/cached report DOM enhancement. Entry used by the lazy retained Paper presenter.
 * Dependencies: sanitized Markdown DOM, explicit translated labels, article-owned lifecycle.
 * Tasks, reading anchors, audit cards and paid generation stay with their existing owners.
 */
import { decorateCallouts } from './callouts';
import { frameFigures, decorateZoomableImages } from './figures';
import { extractGlossary } from './glossary';
import { decorateGlossaryTerms } from './terms';
import { indexHeadings } from './headings';
import type { HeadingEntry } from './contracts';
export { buildReportTableOfContents } from './headings';
export { mountReportArticle, releaseReportArticle, destroyReportArticles } from './lifecycle';

export function enhanceReportArticle(article: HTMLElement,
  translate: (key: 'paper.imageZoomHint') => string): HeadingEntry[] {
  decorateCallouts(article);
  frameFigures(article);
  decorateZoomableImages(article, translate);
  decorateGlossaryTerms(article, extractGlossary(article));
  return indexHeadings(article);
}
