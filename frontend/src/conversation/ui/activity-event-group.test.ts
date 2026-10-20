/** A retry storm collapses to one closed disclosure that keeps its rows. */
import { describe, expect, it, vi } from 'vitest';
import type { TurnActivityEntry } from '../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from '../presentation/conversation-view-model';
import { renderActivityEventGroupBlock } from './renderers/activity-block';
import type { ClassicConversationRendererPorts } from './renderers/ports';
import en from '../../i18n/locales/en.json';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');

function entry(id: string, durationMs: number): TurnActivityEntry {
  return {
    id, spanId: id, seq: 1, occurredAt: 1000, kind: 'model', status: 'failed',
    severity: 'error', count: 1, model: 'deepseek-v4.1-flash-m17',
    statusCode: 429, durationMs,
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
