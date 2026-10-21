// @vitest-environment jsdom
/** Informational reasoning receipts remain outside evidence disclosures in every surface. */
import { afterEach, describe, expect, it } from 'vitest';
import en from '../../../i18n/locales/en.json';
import zh from '../../../i18n/locales/zh.json';
import type { TurnProjection, TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import { selectConversationViewModel } from '../../presentation/conversation-view-model';
import { createClassicConversationRenderers } from '../classic-conversation-renderers';
import { createConversationSurface } from '../conversation-surface';
import { renderDefaultTurnHeader } from '../surface-default-renderers';
import { renderTurnHeader } from './turn-chrome';
import type { ClassicConversationRendererPorts } from './ports';

const receipt = { requestedValue: 'high', actualMode: 'exact', actualValue: 'low' };
const evidence: TurnProjection = { thinkingDepth: 'high', thinking: '',
  lastRoundUsage: { round: 1, reasoningResolution: receipt },
  usage: { completion_tokens_details: { reasoning_tokens: 2843 },
    encrypted_content: 'opaque-replay-never-in-dom', _dispatch: { api_key: 'private-never-in-dom' } },
  segments: [{ type: 'text', blockId: 'text:terminal', text: 'Recorded answer', terminal: true, deliverable: true }],
};
function source(projection: TurnProjection = evidence, status: TurnRecord['status'] = 'completed'): TurnRecord {
  return { turnId: 'reply', presentationId: 'reply', conversationId: 'reasoning-renderer', laneId: 'main',
    ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status, currentAttemptId: null,
    createdAt: 1000, updatedAt: 2000, projectionRevision: 1, settlement: {},
    projection: { content: 'Recorded answer', ...projection } };
}
function stateFor(turn: TurnRecord) {
  const state = createTurnState(turn.conversationId);
  state.turnsById[turn.turnId] = turn;
  state.laneOrder.main = [turn.turnId];
  return state;
}
function ports(locale: 'en' | 'zh' = 'en'): ClassicConversationRendererPorts {
  const messages: Record<string, string> = locale === 'en' ? en : zh;
  return { renderSafeMarkdownHtml(markdown) {
    const p = document.createElement('p'); p.textContent = markdown; return p.outerHTML;
  }, localizedText(key, fallback, values) {
    return (messages[key] ?? fallback).replace(/\{(\w+)\}/g, (token, name: string) =>
      values && Object.hasOwn(values, name) ? String(values[name]) : token);
  } };
}
afterEach(() => document.body.replaceChildren());

describe('common non-expandable reasoning observability', () => {
  it.each(['en', 'zh'] as const)('shows honest completed %s status through the real VM and surface', locale => {
    const container = document.createElement('main'); document.body.append(container);
    const surface = createConversationSurface(container, createClassicConversationRenderers(ports(locale)));
    try {
      surface.render(selectConversationViewModel(stateFor(source())));
      const row = container.querySelector<HTMLElement>('.conversation-reasoning-status')!;
      expect(row).not.toBeNull();
      expect(row.closest('[data-conversation-part="turn-header"]')).not.toBeNull();
      expect(row.closest('details')).toBeNull();
      expect(row.dataset.reasoningSummary).toBe('unavailable');
      expect(row.querySelector('.conversation-reasoning-summary')?.textContent)
        .toBe(locale === 'en' ? 'Reasoning summary unavailable' : '推理摘要不可用');
      expect(row.querySelector('.conversation-reasoning-effort')?.textContent)
        .toBe(locale === 'en' ? 'Last recorded effort: Low' : '最近记录的推理强度：低');
      expect(row.querySelector('.conversation-reasoning-tokens')?.textContent)
        .toBe(locale === 'en' ? 'Provider-reported reasoning tokens: 2,843' : '提供方报告的推理 token：2,843');
      expect(row.querySelector('details, summary, button')).toBeNull();
      expect(container.querySelector('.thinking-block')).toBeNull();
      expect(container.innerHTML).not.toMatch(/opaque-replay-never-in-dom|private-never-in-dom|encrypted_content/);
    } finally { surface.dispose(); }
  });

  it.each(['pending', 'running', 'failed', 'interrupted', 'truncated'] as const)(
    'does not mistake %s for a finished provider summary', status => {
      const vm = selectConversationViewModel(stateFor(source(evidence, status))).mainLane.turns[0];
      const header = document.createElement('header');
      renderTurnHeader(header, vm, ports());
      const row = header.querySelector<HTMLElement>('.conversation-reasoning-status')!;
      expect(row.dataset.reasoningSummary).toBe(status === 'pending' || status === 'running'
        ? 'not-received-yet' : status === 'truncated' ? 'unavailable' : 'not-received');
      expect(row.textContent).not.toMatch(/provider finished|completed thinking|Thinking Process/i);
    });

  it('keeps receipt-only/zero evidence neutral, unknown counts absent and incomplete counts explicit', () => {
    const header = document.createElement('header');
    const select = (projection: TurnProjection) => selectConversationViewModel(stateFor(source(projection))).mainLane.turns[0];
    renderTurnHeader(header, select({ lastRoundUsage: evidence.lastRoundUsage }), ports());
    expect(header.querySelector('.conversation-reasoning-effort')?.textContent).toContain('Low');
    expect(header.querySelector('.conversation-reasoning-tokens')).toBeNull();
    expect(header.querySelector('.conversation-reasoning-summary')).toBeNull();
    renderTurnHeader(header, select({ ...evidence, usage: { reasoning_tokens: 0 } }), ports());
    expect(header.querySelector('.conversation-reasoning-tokens')?.textContent).toContain('0');
    expect(header.querySelector('.conversation-reasoning-summary')).toBeNull();
    renderTurnHeader(header, select({ ...evidence, usageComplete: false }), ports());
    expect(header.querySelector('.conversation-reasoning-tokens')?.textContent).toContain('at least 2,843');
    renderTurnHeader(header, select({ thinkingDepth: 'high' }), ports());
    expect(header.querySelector('.conversation-reasoning-status')).toBeNull();
  });

  it('shares the same status in the default surface rather than a classic-only branch', () => {
    const vm = selectConversationViewModel(stateFor(source())).mainLane.turns[0];
    const header = document.createElement('header');
    renderDefaultTurnHeader(header, vm);
    expect(header.querySelector('.conversation-reasoning-summary')?.textContent).toBe('Reasoning summary unavailable');
    expect(header.querySelector('details')).toBeNull();
  });

  it('repaints late usage/effort without rebuilding an unchanged answer or leaving stale notices', () => {
    const container = document.createElement('main'); document.body.append(container);
    const surface = createConversationSurface(container, createClassicConversationRenderers(ports()));
    const state = stateFor(source({ ...evidence, usage: undefined, lastRoundUsage: undefined }));
    try {
      surface.render(selectConversationViewModel(state));
      const answer = container.querySelector('.md-content')!;
      expect(container.querySelector('.conversation-reasoning-status')).toBeNull();
      state.turnsById.reply = { ...state.turnsById.reply!, projectionRevision: 2,
        projection: { ...state.turnsById.reply!.projection, usage: evidence.usage, lastRoundUsage: evidence.lastRoundUsage } };
      surface.render(selectConversationViewModel(state));
      expect(container.querySelector('.conversation-reasoning-summary')?.textContent).toBe('Reasoning summary unavailable');
      expect(container.querySelector('.md-content')).toBe(answer);
      const row = container.querySelector('.conversation-reasoning-status');
      state.turnsById.reply = { ...state.turnsById.reply!, projectionRevision: 3,
        projection: { ...state.turnsById.reply!.projection,
          lastRoundUsage: { reasoningResolution: { ...receipt, actualValue: 'high' } } } };
      surface.render(selectConversationViewModel(state));
      expect(container.querySelector('.conversation-reasoning-effort')?.textContent).toContain('High');
      expect(container.querySelector('.conversation-reasoning-status')).toBe(row);
      state.turnsById.reply = { ...state.turnsById.reply!, projectionRevision: 4, projection: { content: 'Recorded answer' } };
      surface.render(selectConversationViewModel(state));
      expect(container.querySelector('.conversation-reasoning-status')).toBeNull();
      expect(container.querySelector('.md-content')).toBe(answer);
    } finally { surface.dispose(); }
  });

  it('keeps readable thinking collapse, translation and reader focus unchanged', () => {
    const thinking = { type: 'thinking' as const, blockId: 'readable', text: 'Readable summary',
      translatedText: '可读的摘要', llmRound: 0, terminal: true };
    const turn = source({ ...evidence, thinking: 'Readable summary', segments: [thinking, ...evidence.segments!] });
    const state = stateFor(turn);
    const container = document.createElement('main'); document.body.append(container);
    const surface = createConversationSurface(container, createClassicConversationRenderers(ports('zh')));
    try {
      surface.render(selectConversationViewModel(state));
      expect(container.querySelector('.conversation-reasoning-status')).toBeNull();
      const process = container.querySelector<HTMLDetailsElement>('.conversation-history-process');
      if (process) { process.open = true; process.dispatchEvent(new Event('toggle')); }
      const details = container.querySelector<HTMLDetailsElement>('.thinking-block')!;
      expect(details).not.toBeNull();
      expect(details.open).toBe(false);
      expect(details.querySelector('.thinking-text')?.textContent).toContain('可读的摘要');
      const summary = details.querySelector<HTMLElement>('summary')!;
      details.open = true; summary.tabIndex = 0; summary.focus();
      state.turnsById.reply = { ...turn, projectionRevision: 2,
        projection: { ...turn.projection, usage: { reasoning_tokens: 4000 } } };
      surface.render(selectConversationViewModel(state));
      expect(container.querySelector('.thinking-block')).toBe(details);
      expect(details.open).toBe(true);
      expect(document.activeElement).toBe(summary);
      expect(container.querySelector('.conversation-reasoning-status')).toBeNull();
    } finally { surface.dispose(); }
  });

  it('never interpolates arbitrary effort prose or opaque replay into the DOM', () => {
    const header = document.createElement('header');
    const turn = source({ ...evidence, lastRoundUsage: { reasoningResolution: {
      ...receipt, actualValue: '<img src=x onerror=alert(1)>', reason: 'encrypted_content: opaque-replay-never-in-dom',
    } } });
    renderTurnHeader(header, selectConversationViewModel(stateFor(turn)).mainLane.turns[0], ports());
    expect(header.querySelector('.conversation-reasoning-effort')).toBeNull();
    expect(header.querySelector('.conversation-reasoning-summary')?.textContent).toBe('Reasoning summary unavailable');
    expect(header.innerHTML).not.toMatch(/<img|encrypted_content|opaque-replay-never-in-dom/);
  });
});

it('does not resurrect inherited reasoning information after a real resume ACK or unrelated update', () => {
  const turn = { ...source(), projectionRevision: 7 };
  const state = stateFor(turn);
  const container = document.createElement('main'); document.body.append(container);
  const surface = createConversationSurface(container, createClassicConversationRenderers(ports()));
  try {
    surface.render(selectConversationViewModel(state));
    expect(container.querySelector('.conversation-reasoning-summary')?.textContent).toBe('Reasoning summary unavailable');
    state.attemptsById.resumed = { attemptId: 'resumed', turnId: turn.turnId,
      taskId: 'new-task', operation: 'checkpoint_resume', baseProjectionRevision: 7 };
    state.turnsById.reply = { ...turn, status: 'pending', currentAttemptId: 'resumed', projectionRevision: 8 };
    surface.render(selectConversationViewModel(state));
    expect(container.querySelector('.conversation-reasoning-status')).toBeNull();
    state.turnsById.reply = { ...state.turnsById.reply!, status: 'running', projectionRevision: 9,
      projection: { ...turn.projection, content: 'Unrelated resumed content delta' } };
    surface.render(selectConversationViewModel(state));
    expect(container.querySelector('.conversation-reasoning-status')).toBeNull();
    // An explicitly current API receipt can meter its own returned round, but
    // its colliding round number cannot lend identity to the old effort snapshot.
    state.turnsById.reply = { ...state.turnsById.reply!, projectionRevision: 10,
      projection: { ...state.turnsById.reply!.projection, apiRounds: [{ round: 1,
        attemptId: 'resumed', taskId: 'new-task', usage: { reasoning_tokens: 50 } }] } };
    surface.render(selectConversationViewModel(state));
    expect(container.querySelector('.conversation-reasoning-summary')?.textContent).toBe('No readable reasoning summary received yet');
    expect(container.querySelector('.conversation-reasoning-tokens')?.textContent).toContain('last returned round): 50');
    expect(container.querySelector('.conversation-reasoning-effort')).toBeNull();
    expect(container.querySelector('.thinking-block')).toBeNull();
  } finally { surface.dispose(); }
});


it.each([renderTurnHeader, renderDefaultTurnHeader])(
  'keeps late token fields in fresh-render reading order without replacing retained nodes', render => {
    const select = (turn: TurnRecord) => selectConversationViewModel(stateFor(turn)).mainLane.turns[0];
    const header = document.createElement('header');
    render(header, select(source({ lastRoundUsage: evidence.lastRoundUsage }, 'running')), ports());
    const row = header.querySelector('.conversation-reasoning-status')!;
    const effort = row.querySelector('.conversation-reasoning-effort');
    const summary = row.querySelector('.conversation-reasoning-summary');
    expect(row.children).toHaveLength(2);
    render(header, select(source(evidence)), ports());
    const fresh = document.createElement('header');
    render(fresh, select(source(evidence)), ports());
    expect(Array.from(row.children, child => child.className)).toEqual(
      Array.from(fresh.querySelector('.conversation-reasoning-status')!.children, child => child.className));
    expect(row.textContent).toBe(fresh.querySelector('.conversation-reasoning-status')!.textContent);
    expect(header.querySelector('.conversation-reasoning-status')).toBe(row);
    expect(row.querySelector('.conversation-reasoning-effort')).toBe(effort);
    expect(row.querySelector('.conversation-reasoning-summary')).toBe(summary);
  },
);
