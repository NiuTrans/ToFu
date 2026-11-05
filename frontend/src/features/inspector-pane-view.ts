/** Inspector pane notices and navigation chrome; no reads or retained state.
 * Entries: loading, failure, back bar. Uses shared controls; the caller owns
 * publication rights. Copyable evidence always follows the currently shown pane.
 */
import {createButton} from '../ui';

export type InspectorTranslate = (key: string, values?: Record<string, number>) => string;
export interface InspectorContent extends HTMLElement {
  _rawMessages?: readonly unknown[] | null;
  _rawTools?: unknown;
}

export function inspectorNotice(className: string, text: string): HTMLDivElement {
  const node = document.createElement('div'); node.className = className; node.textContent = text;
  return node;
}

export function inspectorRetry(t: InspectorTranslate, retry: () => void): HTMLButtonElement {
  const button = createButton({label: t('ri.retry'), variant: 'ghost', size: 'sm', onClick: retry});
  button.classList.add('ri-retry');
  return button;
}

export function renderInspectorLoading(content: InspectorContent | null, t: InspectorTranslate): void {
  if (!content) return;
  const notice = inspectorNotice('ri-loading', t('ri.loading'));
  const spinner = document.createElement('span'); spinner.className = 'ri-spinner';
  spinner.setAttribute('aria-hidden', 'true'); notice.prepend(spinner);
  content.replaceChildren(notice); content._rawMessages = null; content._rawTools = null;
}

export function renderInspectorFailure(content: InspectorContent | null, t: InspectorTranslate,
  retry: () => void, className = 'ri-empty'): void {
  if (!content) return;
  const notice = inspectorNotice(className, t('ri.loadFailed'));
  notice.append(inspectorRetry(t, retry));
  content.replaceChildren(notice); content._rawMessages = null; content._rawTools = null;
}

export function renderInspectorBack(content: HTMLElement | null, t: InspectorTranslate, back: () => void): void {
  if (!content) return;
  content.querySelector('.ri-feed-back')?.remove();
  const bar = inspectorNotice('ri-feed-back', '');
  bar.append(createButton({label: t('ri.backToFeed'), variant: 'ghost', size: 'sm', onClick: back}));
  content.prepend(bar);
}
