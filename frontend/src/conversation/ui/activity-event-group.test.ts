/** A retry storm collapses to one closed disclosure that keeps its rows. */
import { describe, expect, it, vi } from 'vitest';
import type { TurnActivityEntry } from '../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from '../presentation/conversation-view-model';
import { renderActivityEventGroupBlock } from './renderers/activity-block';
import type { ClassicConversationRendererPorts } from './renderers/ports';
import en from '../../i18n/locales/en.json';
import zh from '../../i18n/locales/zh.json';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');

function entry(id: string, durationMs: number, envelopeKind?: string): TurnActivityEntry {
  return {
    id, spanId: id, seq: 1, occurredAt: 1000, kind: 'model', status: 'failed',
    severity: 'error', count: 1, model: 'deepseek-v4.1-flash-m17',
    statusCode: 429, durationMs, envelopeKind,
  };
}

function group(items: ReadonlyArray<TurnActivityEntry>):
Extract<ConversationBlockViewModel, { kind: 'activity-event-group' }> {
  return {
    blockId: `activity:${items[0].id}`, kind: 'activity-event-group',
    identitySource: 'contract', source: items[0],
    items: items.map(item => ({
      blockId: `activity:${item.id}`, kind: 'activity-event' as const,
      identitySource: 'contract' as const, source: item, value: item,
    })),
  };
}

function ports(): ClassicConversationRendererPorts {
  return {
    renderSafeMarkdownHtml: () => { throw new Error('Activity evidence is plain text'); },
  } as unknown as ClassicConversationRendererPorts;
}

function mount(items: ReadonlyArray<TurnActivityEntry>) {
  const dom = new JSDOM('<!doctype html><body><div></div></body>');
  const node = dom.window.document.querySelector('div')!;
  const render = () => renderActivityEventGroupBlock(node, group(items), ports());
  render();
  const details = (): HTMLDetailsElement => node.querySelector(
    'details.activity-event-group',
  ) as HTMLDetailsElement;
  return { dom, node, render, details };
}

describe('collapsed retry/failure group', () => {
  it('renders closed with a count, the model and the summed wait', () => {
    const { dom, node, details } = mount([
      entry('a', 556), entry('b', 11000), entry('c', 795),
    ]);
    try {
      expect(details().open).toBe(false);
      const summary = details().querySelector('summary')!;
      expect(summary.textContent).toContain('3 failed or retried requests');
      expect(summary.textContent).toContain('deepseek-v4.1-flash-m17');
      expect(summary.querySelector('.activity-event__duration')!.textContent).toBe('12s');
      // Members are still rendered, unchanged, inside the disclosure.
      expect(details().querySelectorAll('.activity-event')).toHaveLength(3);
    } finally { dom.window.close(); }
  });

  it('names the dominant failure cause instead of only counting the storm', () => {
    const { dom, details } = mount([
      entry('a', 500, 'upstream_error'), entry('b', 700, 'upstream_error'),
      entry('c', 300, 'upstream_error'),
    ]);
    try {
      const causes = details().querySelector('.activity-event-group__causes')!;
      expect(causes.textContent).toBe('Upstream error');
      expect(causes.textContent).not.toContain('×');
    } finally { dom.window.close(); }
  });

  it('shows each cause\'s share of a mixed storm, most frequent first', () => {
    const { dom, details } = mount([
      entry('a', 500, 'upstream_error'), entry('b', 700, 'ratelimit'),
      entry('c', 300, 'ratelimit'),
      // A retry beat carries no classified cause and is never counted as one.
      { ...entry('d', 200), kind: 'status' as const, status: 'waiting' as const,
        phase: 'retrying', envelopeKind: undefined },
    ]);
    try {
      expect(details().querySelector('.activity-event-group__causes')!.textContent)
        .toBe('Rate limited ×2 · Upstream error ×1');
    } finally { dom.window.close(); }
  });

  it('localizes the cause labels', () => {
    const dom = new JSDOM('<!doctype html><body><div></div></body>');
    const node = dom.window.document.querySelector('div')!;
    try {
      renderActivityEventGroupBlock(
        node, group([entry('a', 1, 'upstream_error'), entry('b', 1, 'upstream_error')]),
        {
          renderSafeMarkdownHtml: () => '',
          localizedText: (key: string, fallback: string, values?: Record<string, unknown>) => (
            (zh as Record<string, string>)[key] || fallback
          ).replace(/\{(\w+)\}/g, (match, name) => String(values?.[name] ?? match)),
        } as unknown as ClassicConversationRendererPorts,
      );
      expect(node.querySelector('.activity-event-group__label')!.textContent)
        .toBe('2 次失败 / 重试的请求 · 上游故障');
      expect(node.querySelector('.activity-event__facts')!.textContent)
        .toBe('上游故障 · deepseek-v4.1-flash-m17 · HTTP 429');
      // A member's routing class must not re-contradict the group summary.
      const { dom: classDom, node: classNode } = mount([
        { ...entry('a', 500, 'upstream_error'), reasonCode: 'RateLimitError' },
        entry('b', 700, 'upstream_error'),
      ]);
      try {
        const facts = classNode.querySelectorAll('.activity-event__facts')[0];
        expect(facts.textContent).toBe(
          'Upstream error · deepseek-v4.1-flash-m17 · HTTP 429');
        expect(classNode.textContent).not.toContain('RateLimitError');
        expect(facts.querySelector<HTMLElement>('.activity-event__cause')!.title)
          .toBe('RateLimitError');
      } finally { classDom.window.close(); }
    } finally { dom.window.close(); }
  });

  it('keeps the reader\'s open state across a re-render', () => {
    const { dom, render, details } = mount([entry('a', 556), entry('b', 795)]);
    try {
      details().open = true;
      render();
      expect(details().open).toBe(true);
    } finally { dom.window.close(); }
  });

  it('uses the localized count label, not an English literal', () => {
    const dom = new JSDOM('<!doctype html><body><div></div></body>');
    const node = dom.window.document.querySelector('div')!;
    try {
      renderActivityEventGroupBlock(node, group([entry('a', 1), entry('b', 1)]), {
        renderSafeMarkdownHtml: () => '',
        localizedText: (key: string, fallback: string, values?: Record<string, unknown>) => (
          key === 'activity.retryGroup.summary'
            ? (en as Record<string, string>)[key].replace('{count}', String(values?.count))
            : fallback
        ),
      } as unknown as ClassicConversationRendererPorts);
      expect(node.querySelector('.activity-event-group__label')!.textContent)
        .toBe('2 failed or retried requests');
    } finally { dom.window.close(); }
  });
});
