/** Inline Inspector DOM only. Entries createInspectorInlinePanel, setInspectorInlineStatus,
 * renderInspectorInlineEvidence; uses shared controls/icons and the common Debug renderer.
 * No reads, retained nodes, address resolution, cache or scheduling policy.
 */
import {createIconButton} from '../ui';
import {iconHtml} from '../icons';
import {inspectorRetry} from './inspector-pane-view';
import type {InspectorRef} from './inspector-evidence';
import type {InspectorPayload} from './inspector-payloads';

export type InlineTranslate = (key: string) => string;
export type InlineMessages = InspectorPayload['messages'];
export interface InlineMessageRenderer {
  render(node: HTMLElement, messages: InlineMessages): void;
  characters(message: InlineMessages[number]): number;
  expand(node: HTMLElement): void;
}

export function createInspectorInlinePanel(ref: InspectorRef, t: InlineTranslate, close: () => void): HTMLElement {
  const panel = document.createElement('div');
  panel.className = 'ri-state-panel';
  Object.assign(panel.dataset, {riTask: ref.taskId, riTurn: ref.turn, riRound: String(ref.roundNum), riPanel: ref.taskId + ':' + ref.roundNum});
  panel.innerHTML = '<div class="ri-state-panel-head"><span class="ri-state-panel-kind"></span><span class="ri-state-panel-title"></span></div><div class="ri-state-body"></div>';
  const glyph = document.createElement('span');
  glyph.innerHTML = iconHtml('x', 12);
  glyph.setAttribute('aria-hidden', 'true');
  const button = createIconButton({icon: glyph, label: t('ri.stateClose'), size: 'sm', onClick: close});
  button.classList.add('ri-state-panel-close');
  panel.firstElementChild!.appendChild(button);
  setInspectorInlineStatus(panel, ref, 'ri.loading', t);
  return panel;
}

export function setInspectorInlineStatus(panel: HTMLElement, ref: InspectorRef, key: string, t: InlineTranslate, retry?: () => void): void {
  panel.dataset.riKind = '';
  panel.setAttribute('aria-busy', String(key === 'ri.loading'));
  const kind = panel.querySelector<HTMLElement>('.ri-state-panel-kind')!;
  kind.textContent = ''; kind.classList.remove('ri-kind-fallback'); kind.title = '';
  panel.querySelector<HTMLElement>('.ri-state-panel-title')!.textContent = 'R' + ref.roundNum;
  const body = panel.querySelector<HTMLElement>('.ri-state-body')!;
  const notice = document.createElement('div'); notice.className = 'ri-empty'; notice.textContent = t(key);
  body.replaceChildren(notice);
  if (retry) body.appendChild(inspectorRetry(t, retry));
}

export function renderInspectorInlineEvidence(panel: HTMLElement, ref: InspectorRef, kind: string,
  payload: InspectorPayload, shown: InlineMessages, t: InlineTranslate, renderer: InlineMessageRenderer): void {
  panel.dataset.riKind = kind;
  panel.setAttribute('aria-busy', 'false');
  const badge = panel.querySelector<HTMLElement>('.ri-state-panel-kind')!;
  badge.textContent = t(kind === 'state' ? 'ri.tabState' : 'ri.tabRequest');
  badge.classList.toggle('ri-kind-fallback', kind !== 'state');
  badge.title = t(kind === 'state' ? 'ri.stateKindTip' : 'ri.requestKindTip');
  panel.querySelector<HTMLElement>('.ri-state-panel-title')!.textContent =
    (payload.label || ('R' + ref.roundNum)) + ' · +' + shown.length + ' msgs';
  const body = panel.querySelector<HTMLElement>('.ri-state-body')!;
  renderer.render(body, shown);
  // Existing reading policy: reveal small increments; large evidence remains
  // complete and collapsed. Tool schemas belong to the full drawer detail.
  const characters = shown.reduce((total, message) => total + renderer.characters(message), 0);
  if (shown.length <= 6 && characters <= 300 * 1024) {
    body.querySelectorAll<HTMLElement>('.debug-msg-block').forEach(node => renderer.expand(node));
  }
}
