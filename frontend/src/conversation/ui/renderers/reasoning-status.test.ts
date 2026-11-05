// @vitest-environment jsdom
/** Reply headers stay content-first; reasoning evidence remains available to diagnostics. */
import { afterEach, describe, expect, it, vi } from 'vitest';
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

const receipt = { requestedValue: 'high', actualMode: 'exact', actualValue: 'max' };
const evidence: TurnProjection = { thinkingDepth: 'high', thinking: '',
  lastRoundUsage: { round: 1, reasoningResolution: receipt },
  usage: { completion_tokens_details: { reasoning_tokens: 2002 },
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
function expectNoReasoningNotice(container: HTMLElement) {
  expect(container.querySelector('.conversation-reasoning-status, [data-reasoning-summary]')).toBeNull();
  expect(container.textContent).not.toMatch(/Last recorded effort|Provider-reported reasoning tokens|Reasoning summary unavailable|最近记录的推理强度|提供方报告的推理 token|推理摘要不可用/);
}
afterEach(() => document.body.replaceChildren());

describe('no unsolicited reasoning status in reply headers', () => {
  it.each(['en', 'zh'] as const)('keeps the %s answer visible without a receipt or empty thinking card', locale => {
    const container = document.createElement('main'); document.body.append(container);
    const surface = createConversationSurface(container, createClassicConversationRenderers(ports(locale)));
    const turn = source();
    const before = JSON.stringify(turn);
    try {
      surface.render(selectConversationViewModel(stateFor(turn)));
      expectNoReasoningNotice(container);
      expect(container.querySelector('.thinking-block')).toBeNull();
      expect(container.querySelector('.md-content')?.textContent).toBe('Recorded answer');
      expect(container.innerHTML).not.toMatch(/opaque-replay-never-in-dom|private-never-in-dom|encrypted_content/);
      expect(JSON.stringify(turn)).toBe(before);
    } finally { surface.dispose(); }
  });

  it.each(['pending', 'running', 'completed', 'failed', 'interrupted', 'truncated'] as const)(
    'omits reasoning notices in both headers for %s replies', status => {
      const vm = selectConversationViewModel(stateFor(source(evidence, status))).mainLane.turns[0];
      for (const render of [renderTurnHeader, renderDefaultTurnHeader]) {
        const header = document.createElement('header');
        render(header, vm, ports());
        expectNoReasoningNotice(header);
        expect(header.children.length).toBeGreaterThan(0);
      }
    });

  it.each([
    { lastRoundUsage: evidence.lastRoundUsage },
    { ...evidence, usage: { reasoning_tokens: 0 } },
    { ...evidence, usageComplete: false },
    { thinkingDepth: 'high' },
    { apiRounds: [{ round: 1, usage: { reasoning_tokens: 50, _reasoning_resolution: receipt } }] },
  ] as TurnProjection[])('does not substitute receipt-only, partial or legacy evidence with chrome: %j', projection => {
    const vm = selectConversationViewModel(stateFor(source(projection))).mainLane.turns[0];
    const header = document.createElement('header');
    renderTurnHeader(header, vm, ports());
    expectNoReasoningNotice(header);
  });

  it.each(['classic', 'default'] as const)('does not repaint the %s header or replace an answer for late metering', mode => {
    const container = document.createElement('main'); document.body.append(container);
    const renderHeader = mode === 'classic'
      ? vi.fn((node: HTMLElement, turn: Parameters<typeof renderTurnHeader>[1]) => renderTurnHeader(node, turn, ports()))
      : vi.fn(renderDefaultTurnHeader);
    const options = mode === 'classic'
      ? { ...createClassicConversationRenderers(ports()), renderTurnHeader: renderHeader }
      : { renderTurnHeader: renderHeader };
    const surface = createConversationSurface(container, options);
    const state = stateFor(source({ ...evidence, usage: undefined, lastRoundUsage: undefined }));
    try {
      surface.render(selectConversationViewModel(state));
      const selector = mode === 'classic' ? '.md-content' : '.conversation-block__markdown';
      const answer = container.querySelector(selector);
      expect(answer).not.toBeNull();
      expect(renderHeader).toHaveBeenCalledTimes(1);
      state.turnsById.reply = { ...state.turnsById.reply!, projectionRevision: 2,
        projection: { ...state.turnsById.reply!.projection, usage: evidence.usage, lastRoundUsage: evidence.lastRoundUsage } };
      surface.render(selectConversationViewModel(state));
      expectNoReasoningNotice(container);
      expect(container.querySelector(selector)).toBe(answer);
      expect(renderHeader).toHaveBeenCalledTimes(1);
      state.turnsById.reply = { ...state.turnsById.reply!, projectionRevision: 3,
        projection: { ...state.turnsById.reply!.projection,
          lastRoundUsage: { reasoningResolution: { ...receipt, actualValue: 'high' } } } };
      surface.render(selectConversationViewModel(state));
      expectNoReasoningNotice(container);
      expect(container.querySelector(selector)).toBe(answer);
      expect(renderHeader).toHaveBeenCalledTimes(1);
    } finally { surface.dispose(); }
  });

  it('preserves readable thinking, translation, open state and reader focus across usage updates', () => {
    const thinking = { type: 'thinking' as const, blockId: 'readable', text: 'Readable summary',
      translatedText: '可读的摘要', llmRound: 0, terminal: true };
    const turn = source({ ...evidence, thinking: 'Readable summary', segments: [thinking, ...evidence.segments!] });
    const state = stateFor(turn);
    const container = document.createElement('main'); document.body.append(container);
    const surface = createConversationSurface(container, createClassicConversationRenderers(ports('zh')));
    try {
      surface.render(selectConversationViewModel(state));
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
      expectNoReasoningNotice(container);
    } finally { surface.dispose(); }
  });

  it('does not expose arbitrary effort strings or encrypted replay in the header', () => {
    const turn = source({ ...evidence, lastRoundUsage: { reasoningResolution: {
      ...receipt, actualValue: '<img src=x onerror=alert(1)>', reason: 'opaque-replay-never-in-dom',
    } } });
    const header = document.createElement('header');
    renderTurnHeader(header, selectConversationViewModel(stateFor(turn)).mainLane.turns[0], ports());
    expectNoReasoningNotice(header);
    expect(header.innerHTML).not.toMatch(/<img|encrypted_content|opaque-replay-never-in-dom|private-never-in-dom/);
  });

  it('does not add a notice when a resumed attempt receives new scoped metering', () => {
    const turn = { ...source(), projectionRevision: 7 };
    const state = stateFor(turn);
    const container = document.createElement('main'); document.body.append(container);
    const surface = createConversationSurface(container, createClassicConversationRenderers(ports()));
    try {
      surface.render(selectConversationViewModel(state));
      expectNoReasoningNotice(container);
      state.attemptsById.resumed = { attemptId: 'resumed', turnId: turn.turnId,
        taskId: 'new-task', operation: 'checkpoint_resume', baseProjectionRevision: 7 };
      state.turnsById.reply = { ...turn, status: 'pending', currentAttemptId: 'resumed', projectionRevision: 8 };
      surface.render(selectConversationViewModel(state));
      expectNoReasoningNotice(container);
      state.turnsById.reply = { ...state.turnsById.reply!, status: 'running', projectionRevision: 9,
        projection: { ...turn.projection, apiRounds: [{ round: 1,
          attemptId: 'resumed', taskId: 'new-task', usage: { reasoning_tokens: 50 } }] } };
      surface.render(selectConversationViewModel(state));
      expectNoReasoningNotice(container);
      expect(container.querySelector('.thinking-block')).toBeNull();
    } finally { surface.dispose(); }
  });
});
