/** Compact, non-expandable reasoning observations for classic/default headers.
 * Only derived enums and validated counts reach text nodes; opaque provider
 * evidence never reaches DOM. Entry: renderReasoningStatus. Dependencies:
 * presentation types, injected copy and existing retained-node primitives. */
import type { ConversationTurnViewModel, TurnReasoningViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { directClassChild } from './stable-dom';
import { textFor } from './localized-copy';

type CopyPorts = Pick<ClassicConversationRendererPorts, 'localizedText'>;
const effortCopy: Record<NonNullable<TurnReasoningViewModel['effort']>, [string, string]> = {
  minimal: ['chat.reasoning.effort.minimal', 'Minimal'],
  low: ['chat.reasoning.effort.low', 'Low'],
  medium: ['chat.reasoning.effort.medium', 'Medium'],
  high: ['chat.reasoning.effort.high', 'High'],
  xhigh: ['chat.reasoning.effort.xhigh', 'Extra high'],
  max: ['chat.reasoning.effort.max', 'Max'],
  off: ['chat.reasoning.effort.off', 'Off'],
  enabled: ['chat.reasoning.effort.enabled', 'Enabled'],
};

/** Returns the retained row for the header's existing replaceChildren owner. */
export function renderReasoningStatus(
  header: HTMLElement,
  turn: ConversationTurnViewModel,
  ports: CopyPorts = {},
): HTMLElement | null {
  const value = turn.metadata.reasoning;
  if (!value || value.summary === 'readable' || turn.actor === 'human' || turn.commandPending) return null;
  const labels: Array<[string, string]> = [];
  if (value.effort && Object.hasOwn(effortCopy, value.effort)) {
    const [key, fallback] = effortCopy[value.effort];
    labels.push(['conversation-reasoning-effort', textFor(ports,
      'chat.reasoning.lastRecordedEffort', 'Last recorded effort: {effort}',
      { effort: textFor(ports, key, fallback) })]);
  }
  if (typeof value.tokens === 'number' && Number.isSafeInteger(value.tokens) && value.tokens >= 0) {
    const lastRound = value.tokensSource === 'last-round';
    const [key, fallback] = lastRound
      ? value.tokensLowerBound
        ? ['chat.reasoning.lastRoundTokensLowerBound', 'Provider-reported reasoning tokens (last returned round): at least {tokens}']
        : ['chat.reasoning.lastRoundTokens', 'Provider-reported reasoning tokens (last returned round): {tokens}']
      : value.tokensLowerBound
        ? ['chat.reasoning.reportedTokensLowerBound', 'Provider-reported reasoning tokens: at least {tokens}']
        : ['chat.reasoning.reportedTokens', 'Provider-reported reasoning tokens: {tokens}'];
    labels.push(['conversation-reasoning-tokens', textFor(ports, key, fallback,
      { tokens: value.tokens.toLocaleString('en-US') })]);
  }
  const summaryCopy = value.summary === 'unavailable'
    ? ['chat.reasoning.summaryUnavailable', 'Reasoning summary unavailable']
    : value.summary === 'not-received-yet'
      ? ['chat.reasoning.summaryPending', 'No readable reasoning summary received yet']
      : value.summary === 'not-received'
        ? ['chat.reasoning.summaryNotReceived', 'No readable reasoning summary received'] : null;
  if (summaryCopy) labels.push(['conversation-reasoning-summary', textFor(ports, summaryCopy[0], summaryCopy[1])]);
  if (!labels.length) return null;
  const row = directClassChild<HTMLElement>(header, 'conversation-reasoning-status')
    ?? header.ownerDocument.createElement('div');
  row.className = 'conversation-reasoning-status';
  row.setAttribute('role', 'status');
  row.setAttribute('aria-live', turn.status === 'pending' || turn.status === 'running' ? 'polite' : 'off');
  row.setAttribute('aria-atomic', 'true');
  if (summaryCopy) row.dataset.reasoningSummary = value.summary!;
  else delete row.dataset.reasoningSummary;
  const keep = new Set(labels.map(([className]) => className));
  for (const child of Array.from(row.children)) {
    if (!keep.has(child.className)) child.remove();
  }
  for (const [index, [className, label]] of labels.entries()) {
    let child = directClassChild<HTMLElement>(row, className);
    if (!child) {
      child = header.ownerDocument.createElement('span');
      child.className = className;
    }
    const position = row.children.item(index);
    if (position !== child) row.insertBefore(child, position);
    if (child.textContent !== label) child.textContent = label;
  }
  return row;
}
