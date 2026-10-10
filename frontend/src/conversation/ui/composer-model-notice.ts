/**
 * Model identity for the fixed composer area, including live fallback decisions.
 * Entry point: renderComposerModelNotice. Reads authoritative Turn projection and
 * metadata; depends on the shared serving-route resolver and localization port.
 */
import type { ConversationTurnViewModel, ConversationViewModel } from '../presentation/conversation-view-model';
import { resolveTurnServingRoute } from '../presentation/turn-serving-route';
import type { ClassicConversationRendererPorts } from './renderers/ports';
import { textFor } from './renderers/localized-copy';

function appendTurnModelNotice(
  node: HTMLElement,
  turn: ConversationTurnViewModel,
  ports: Pick<ClassicConversationRendererPorts, 'localizedText'>,
): void {
  if (turn.actor === 'human') return;
  const route = resolveTurnServingRoute(turn.source.projection);
  const fallback = turn.metadata.fallback;
  const switched = Boolean(fallback?.model && fallback.from
    && fallback.model !== fallback.from);
  if (!switched && (!route.model || route.source === 'projection')) return;
  const document = node.ownerDocument;
  const notice = document.createElement('span');
  notice.className = switched
    ? 'conversation-model-notice conversation-model-notice--switched'
    : 'conversation-model-notice';
  const label = document.createElement('span');
  label.className = 'conversation-model-notice-label';
  label.textContent = switched
    ? textFor(ports, 'turnModel.switched', 'Automatically switched: {from} → {to}', {
      from: fallback!.from!, to: fallback!.model!,
    })
    : textFor(ports, 'turnModel.actual', 'Model: {model}', { model: route.model });
  notice.appendChild(label);
  if (switched) {
    // The decision event arrives before the first fallback response. Preserve
    // its target even when the previous round still reports the original route.
    notice.setAttribute('role', 'status');
    const reason = document.createElement('span');
    reason.className = 'conversation-model-notice-reason';
    const detail = fallback?.reason?.replace(/\s+/g, ' ').trim();
    reason.textContent = detail
      ? textFor(ports, 'turnModel.reason', 'Reason: {reason}', { reason: detail })
      : textFor(ports, 'turnModel.reasonUnknown', 'The previous model request failed.');
    notice.appendChild(reason);
    if (route.model && route.model !== fallback!.from
      && (route.logicalModel === fallback!.model || route.model === fallback!.model)) {
      const actual = document.createElement('span');
      actual.className = 'conversation-model-notice-route';
      actual.textContent = textFor(ports, 'turnModel.actual', 'Model: {model}', {
        model: route.providerId ? `${route.model} · ${route.providerId}` : route.model,
      });
      notice.appendChild(actual);
    }
  }
  node.appendChild(notice);
}

/** Latest main-lane turn only: a new human turn must never inherit old routing. */
export function renderComposerModelNotice(
  node: HTMLElement | null,
  viewModel: ConversationViewModel | null,
  ports: Pick<ClassicConversationRendererPorts, 'localizedText'> = {},
): void {
  if (!node) return;
  const turn = viewModel?.mainLane.turns.at(-1);
  const content = node.ownerDocument.createElement('div');
  if (turn) appendTurnModelNotice(content, turn, ports);
  // Rendering-local identity preserves the status node during stream updates,
  // so screen readers are not repeatedly notified by unrelated token changes.
  const signature = JSON.stringify([viewModel?.conversationId, turn?.turnId, content.textContent]);
  if (node.dataset.modelNoticeSignature === signature) return;
  node.dataset.modelNoticeSignature = signature;
  node.dataset.conversationId = viewModel?.conversationId ?? '';
  node.replaceChildren(...Array.from(content.childNodes));
  node.hidden = node.childElementCount === 0;
}
