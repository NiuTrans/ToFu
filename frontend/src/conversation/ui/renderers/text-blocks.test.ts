// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import type { LiveStatusBlockViewModel, TextBlockViewModel, ThinkingBlockViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { renderLiveStatusBlock, renderTextBlock, renderThinkingBlock } from './text-blocks';

afterEach(() => document.body.replaceChildren());
function fixture(overrides: Partial<TextBlockViewModel> = {}) {
  const node = document.createElement('div');
  document.body.appendChild(node);
  const block: TextBlockViewModel = { kind: 'text', blockId: 'answer', source: {}, identitySource: 'contract',
    markdown: 'Original body', translatedMarkdown: 'Translated body', displayMarkdown: 'Original body',
    displayMode: 'original', translationModel: 'gpt-6-luna', terminal: true, deliverable: true,
    resumable: false, ...overrides };
  const ports: ClassicConversationRendererPorts = { renderSafeMarkdownHtml: text => {
    const safe = document.createElement('p'); safe.textContent = text; return safe.outerHTML;
  } };
  renderTextBlock(node, block, ports);
  return { node, block, ports };
}

describe('message translation attribution', () => {
  it('places the actual model beside the Translation disclosure without opening it', () => {
    const { node } = fixture();
    const details = node.querySelector('details')!;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toContain('Translation');
    expect(details.querySelector('.bilingual-model')?.textContent).toBe('Translated by gpt-6-luna');
    expect(node.querySelector('.bilingual-attribution')).toBeNull();
  });

  it('attributes the visible translated answer once while retaining the Original fold, reader state and focus', () => {
    const { node, block, ports } = fixture();
    const details = node.querySelector('details')!;
    const summary = details.querySelector('summary')!;
    summary.tabIndex = 0; summary.focus(); details.open = true;
    renderTextBlock(node, { ...block, displayMode: 'translated', displayMarkdown: 'Translated body' }, ports);
    expect(node.querySelector('.md-content')?.textContent).toBe('Translated body');
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe('Translated by gpt-6-luna');
    expect(summary.textContent).toContain('Original');
    expect(summary.textContent).not.toContain('gpt-6-luna');
    expect(node.querySelectorAll('.bilingual-model')).toHaveLength(1);
    expect(node.querySelector('details')).toBe(details);
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(summary);
    renderTextBlock(node, block, ports);
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(summary);
    expect(node.querySelector('.bilingual-attribution')).toBeNull();
  });

  it('shows a canonical receipt without manufacturing an alternate-body panel', () => {
    const { node } = fixture({ translatedMarkdown: undefined, inputTranslationCompleted: true });
    expect(node.querySelector('details')).toBeNull();
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe('Translated by gpt-6-luna');
    expect(node.querySelector('.md-content')?.textContent).toBe('Original body');
  });

  it('uses localization and safe plain text for arbitrary model IDs, then removes stale attribution', () => {
    const model = '<img src=x onerror=alert(1)>/provider:model';
    const { node, block, ports } = fixture({ translationModel: model, displayMode: 'translated' });
    const localizedText = vi.fn((key: string, fallback: string, values?: Readonly<Record<string, string | number>>) =>
      key === 'chat.translationModel' ? `翻译模型：${values?.model}` : fallback);
    renderTextBlock(node, block, { ...ports, localizedText });
    expect(localizedText).toHaveBeenCalledWith('chat.translationModel', 'Translated by {model}', { model });
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe(`翻译模型：${model}`);
    expect(node.querySelector('.bilingual-attribution')?.getAttribute('title')).toBe(`翻译模型：${model}`);
    expect(node.querySelector('summary .bilingual-model')).toBeNull();
    expect(node.querySelector('img')).toBeNull();
    renderTextBlock(node, { ...block, translationModel: undefined }, ports);
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe('Translation model not reported');
  });

  it('makes missing attribution explicit for legacy translations without guessing a model', () => {
    const { node } = fixture({ translationModel: undefined });
    expect(node.querySelector('summary')?.textContent).toContain('Translation model not reported');
  });

  it('does not label original-only content from model metadata alone', () => {
    const { node } = fixture({ translatedMarkdown: undefined });
    expect(node.querySelector('.bilingual-model')).toBeNull();
    expect(node.querySelector('details')).toBeNull();
  });
});

describe('live status elapsed clock', () => {
  afterEach(() => vi.useRealTimers());
  it('ticks from the backend call start outside the announced phase text', () => {
    vi.useFakeTimers({ now: 1_793_693_000_000 + 125_000, toFake: ['Date', 'setInterval', 'clearInterval'] });
    const node = document.createElement('div');
    node.className = 'conversation-block conversation-block--live-status';
    document.body.appendChild(node);
    const catalog: Record<string, string> = { 'stream.phase.runningToolIntent': 'Running {tool}: {intent}' };
    const ports: ClassicConversationRendererPorts = { renderSafeMarkdownHtml: text => text,
      localizedText: (key, fallback, values) => (catalog[key] ?? fallback)
        .replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? '')) };
    const block = (value: Partial<LiveStatusBlockViewModel['value']>): LiveStatusBlockViewModel => ({
      kind: 'live-status', blockId: 'live-status', source: {}, identitySource: 'contract',
      value: { kind: 'attempt', phase: 'tool_exec', label: '', detail: '', tools: ['run_command'],
        detailKey: 'stream.phase.runningToolIntent',
        detailArgs: { tool: 'run_command', intent: 'Run the slides tests' }, ...value },
    });
    renderLiveStatusBlock(node, block({ startedAt: 1_793_693_000_000 }), ports);
    const clock = node.querySelector<HTMLElement>('.stream-phase-elapsed')!;
    expect(node.querySelector('.stream-phase-text')?.firstChild?.textContent)
      .toBe('Running run_command: Run the slides tests');
    expect(clock.textContent).toBe('02:05');
    expect(clock.getAttribute('aria-hidden')).toBe('true');
    expect(node.getAttribute('aria-live')).toBe('polite');
    vi.advanceTimersByTime(2_000);
    expect(clock.textContent).toBe('02:07');
    renderLiveStatusBlock(node, block({ phase: 'thinking_active', tools: undefined,
      detailKey: undefined, detailArgs: undefined }), ports);
    expect(node.querySelector('.stream-phase-elapsed')).toBeNull();
    expect(node.querySelector('.stream-phase-text')?.textContent).toBe('Thinking…');
  });
});

describe('live status wait tick', () => {
  let render: typeof renderLiveStatusBlock;
  beforeEach(async () => {
    // The tick interval is module state; a fresh module owns this test's fake clock.
    vi.resetModules();
    ({ renderLiveStatusBlock: render } = await import('./text-blocks'));
  });
  afterEach(() => vi.useRealTimers());

  it('advances the elapsed argument even when a name placed before it contains its digits', () => {
    vi.useFakeTimers({ now: 1_793_693_000_000, toFake: ['Date', 'setInterval', 'clearInterval'] });
    const node = document.createElement('div');
    node.className = 'conversation-block conversation-block--live-status';
    document.body.appendChild(node);
    // Locales such as ja/ko/de put the name first.
    const catalog: Record<string, string> = {
      'stream.phase.preparingToolCall': '{tool} の呼び出しを準備中（{elapsed}秒）',
      'stream.phase.waitingForResponse': '{model}の応答を待っています（{elapsed}秒経過）…',
    };
    const ports: ClassicConversationRendererPorts = { renderSafeMarkdownHtml: text => text,
      localizedText: (key, fallback, values) => (catalog[key] ?? fallback)
        .replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? '')) };
    const block = (value: Partial<LiveStatusBlockViewModel['value']>): LiveStatusBlockViewModel => ({
      kind: 'live-status', blockId: 'live-status', source: {}, identitySource: 'contract',
      value: { kind: 'attempt', phase: 'llm_thinking', label: '', detail: '', ...value },
    });
    const text = () => node.querySelector('.stream-phase-text')?.textContent;
    render(node, block({ detailKey: 'stream.phase.preparingToolCall',
      detailArgs: { model: 'm', tool: 'fetch_v2', elapsed: 2 } }), ports);
    expect(text()).toBe('fetch_v2 の呼び出しを準備中（2秒）');
    vi.advanceTimersByTime(3_000);
    expect(text()).toBe('fetch_v2 の呼び出しを準備中（5秒）');
    render(node, block({ phase: 'waiting_model', detailKey: 'stream.phase.waitingForResponse',
      detailArgs: { model: 'claude-opus-5.5', elapsed: 5 } }), ports);
    vi.advanceTimersByTime(2_000);
    expect(text()).toBe('claude-opus-5.5の応答を待っています（7秒経過）…');
  });
});

it('retains focused evidence through answer replay and an appended paragraph', () => {
  const node = document.createElement('section');
  document.body.append(node);
  const turn = { actor: 'assistant', role: 'assistant', source: { projection: {} } } as ConversationTurnViewModel;
  const block = { kind: 'text', blockId: 'answer', markdown: 'original', terminal: true,
    deliverable: true } as TextBlockViewModel;
  const ports = { renderSafeMarkdownHtml: (text: string) =>
    '<p>Answer</p><pre><button type="button">Copy code</button><code>const value = 1;</code></pre>'
      + (text === 'extended' ? '<p>Further explanation.</p>' : '') };
  try {
    renderTextBlock(node, block, ports, turn);
    const control = node.querySelector<HTMLButtonElement>('pre button')!;
    control.focus();
    for (const markdown of ['original', 'extended']) {
      renderTextBlock(node, { ...block, markdown }, ports, turn);
      expect(document.activeElement).toBe(control);
      expect(node.querySelector('pre button')).toBe(control);
      expect(node.querySelectorAll('.conversation-answer-label')).toHaveLength(1);
    }
    renderTextBlock(node, { ...block, deliverable: false }, ports, turn);
    expect(node.querySelector('.conversation-answer-label')).toBeNull();
  } finally { node.remove(); }
});

it('folds merged reasoning after each round unless the reader opened it', () => {
  const node = document.createElement('div');
  document.body.appendChild(node);
  const ports: ClassicConversationRendererPorts = { renderSafeMarkdownHtml: text => text };
  const thinking = (markdown: string, terminal: boolean): ThinkingBlockViewModel => ({
    kind: 'thinking', blockId: 'think-0', source: {}, identitySource: 'contract', memberBlockIds: ['think-0'],
    markdown, displayMarkdown: markdown, displayMode: 'original', terminal,
  });
  renderThinkingBlock(node, thinking('Round one', false), ports);
  const details = node.querySelector<HTMLDetailsElement>('details.thinking-block')!;
  expect(details.open).toBe(true);
  const glyph = details.querySelector<HTMLElement>('.thinking-icon')!;
  expect(glyph.getAttribute('aria-hidden')).toBe('true');
  expect(glyph.querySelector('svg')).not.toBeNull();
  expect(details.querySelector('.thinking-state-dot')).toBeNull();
  renderThinkingBlock(node, thinking('Round one', true), ports);
  expect(details.querySelector('.thinking-icon')).toBe(glyph);
  expect(details.open).toBe(false);

  // The reader opens it; the next round streams into the same disclosure and
  // settles without snapping it shut.
  details.querySelector<HTMLElement>('summary')!.click();
  expect(details.open).toBe(true);
  renderThinkingBlock(node, thinking('Round one\n\nRound two', false), ports);
  renderThinkingBlock(node, thinking('Round one\n\nRound two', true), ports);
  expect(node.querySelector('details.thinking-block')).toBe(details);
  expect(details.open).toBe(true);
});

it('makes open reasoning a labelled, focusable scroller that rests at its newest line', () => {
  const node = document.createElement('div');
  document.body.appendChild(node);
  const ports: ClassicConversationRendererPorts = { renderSafeMarkdownHtml: text => text };
  const thinking = (markdown: string, terminal: boolean): ThinkingBlockViewModel => ({
    kind: 'thinking', blockId: 'think-0', source: {}, identitySource: 'contract', memberBlockIds: ['think-0'],
    markdown, displayMarkdown: markdown, displayMode: 'original', terminal,
  });
  try {
    renderThinkingBlock(node, thinking('Line one', false), ports);
    const scroller = node.querySelector<HTMLElement>('.thinking-content')!;
    expect(scroller.tabIndex).toBe(0);
    expect(scroller.getAttribute('role')).toBe('region');
    expect(scroller.getAttribute('aria-label'))
      .toBe(node.querySelector('.thinking-label')!.textContent);
    // jsdom has no layout: a reader at the end (0 remaining) stays pinned.
    let top = 0;
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 900 });
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 300 });
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: value => { top = value; } });
    top = 600;
    renderThinkingBlock(node, thinking('Line one\n\nLine two', false), ports);
    expect(top).toBe(900);
    // A reader who scrolled up keeps their place.
    top = 100;
    scroller.dispatchEvent(new Event('scroll'));
    renderThinkingBlock(node, thinking('Line one\n\nLine two\n\nLine three', false), ports);
    expect(top).toBe(100);
    // Reopening starts at the newest line again.
    const details = node.querySelector<HTMLDetailsElement>('.thinking-block')!;
    details.open = false;
    details.dispatchEvent(new Event('toggle'));
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    expect(top).toBe(900);
  } finally { node.remove(); }
});
