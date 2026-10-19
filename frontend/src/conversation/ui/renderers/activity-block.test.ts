/** Compaction summary evidence is distinct from the main turn model. */
import { describe, expect, it, vi } from 'vitest';
import type { TurnActivityEntry } from '../../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import { renderActivityEventBlock } from './activity-block';
import type { ClassicConversationRendererPorts } from './ports';
import en from '../../../i18n/locales/en.json';
import zh from '../../../i18n/locales/zh.json';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');

function render(
  extra: Partial<TurnActivityEntry>,
  localizedText?: ClassicConversationRendererPorts['localizedText'],
) {
  const dom = new JSDOM('<!doctype html><body><div></div></body>');
  const node = dom.window.document.querySelector('div')!;
  const entry: TurnActivityEntry = {
    id: 'compact', spanId: 'compact', seq: 1, occurredAt: 1000,
    kind: 'status', status: 'succeeded', severity: 'info',
    summary: 'Context compaction', count: 1, reasonCode: 'context_compaction',
    archiveId: 'archive', model: 'Sol', ...extra,
  };
  const update = (changes: Partial<TurnActivityEntry> = {}) => {
    Object.assign(entry, changes);
    renderActivityEventBlock(node, {
      kind: 'activity-event', blockId: entry.id, identitySource: 'contract',
      value: entry, source: entry,
    } as Extract<ConversationBlockViewModel, {kind: 'activity-event'}>, {
      renderSafeMarkdownHtml: () => { throw new Error('Attribution must be plain text'); },
      localizedText,
    });
  };
  update();
  return { dom, node, update };
}

describe('compaction summary model provenance', () => {
  it('shows summary execution while keeping unchanged main-model diagnostics folded', () => {
    const {dom, node} = render({taskModel: 'Sol', summaryModel: 'Luna',
      summarySelectedModel: 'Luna', summaryProviderId: 'summary-provider'});
    try {
      expect(node.textContent).toContain('Summary model: Luna');
      expect(node.textContent).toContain('Main model: Sol');
      expect(node.textContent).toContain('summary-provider');

      expect(node.querySelector<HTMLDetailsElement>('details')?.open).toBe(false);
      const primary = node.querySelector('.activity-event__body > .activity-event__facts')!;
      expect(primary.textContent).toContain('Summary model: Luna');
      expect(primary.textContent).not.toContain('Sol');
      expect(primary.textContent).not.toContain('summary-provider');
      expect(node.querySelector('button')?.dataset.operation).toBe('archive');
    } finally { dom.window.close(); }
  });

  it('does not reinterpret historical parent model/provider as the summary', () => {
    const {dom, node} = render({providerId: 'parent-provider'});
    try {
      expect(node.textContent).toContain('Summary model not recorded');
      expect(node.textContent).toContain('Main model: Sol');
      expect(node.textContent).not.toContain('Summary model: Sol');
      expect(node.textContent).not.toContain('parent-provider');
    } finally { dom.window.close(); }
  });

  it('labels selection-only evidence honestly, including failed summaries', () => {
    const {dom, node} = render({status: 'failed', summarySelectedModel: 'chosen-Luna'});
    try {
      expect(node.textContent).toContain('Summary model not recorded');
      expect(node.textContent).toContain('Selected summary model: chosen-Luna');
      expect(node.textContent).not.toContain('Summary model: chosen-Luna');
    } finally { dom.window.close(); }
  });
});


it('shows distinct recorded retry models rather than a false single-model attribution', () => {
  const {dom, node} = render({status: 'failed', summaryCalls: [
    {model: 'Luna', providerId: 'one', status: 'failed'},
    {model: 'different-Luna', providerId: 'two', status: 'failed'},
  ]});
  try {
    expect(node.textContent).toContain('Summary model: Luna');
    expect(node.textContent).toContain('Summary model: different-Luna');
    expect(node.textContent).toContain('Main model: Sol');
    expect(node.textContent).not.toContain('Summary model not recorded');
  } finally { dom.window.close(); }
});


describe('compact compression diagnostics', () => {
  it.each([
    ['working_set', 'Long context'],
    ['window', 'Context budget threshold reached'],
    ['dispatch_guard', 'Request too large before sending'],
    ['context_overflow', 'Provider context overflow'],
    ['manual', 'Manual compression'],
  ])('uses a short label for %s, retaining the original reason', (trigger, label) => {
    const {dom, node} = render({trigger,
      detail: 'estimated input crossed 244,800-token working_set threshold'});
    try {
      expect(node.querySelector('.activity-event__body > .activity-event__facts')?.textContent)
        .toContain(label);
      expect(node.querySelector('.activity-event__body > .activity-event__detail')).toBeNull();
      expect(node.querySelector('details .activity-event__detail')?.textContent)
        .toBe('estimated input crossed 244,800-token working_set threshold');
    } finally { dom.window.close(); }
  });

  it.each([
    ['en', en, {
      working_set: 'Long context',
      window: 'Context budget threshold reached',
      dispatch_guard: 'Request too large before sending',
      context_overflow: 'Provider context overflow',
      manual: 'Manual compression',
    }],
    ['zh', zh, {
      working_set: '上下文较长',
      window: '达到上下文预算阈值',
      dispatch_guard: '发送前请求过大',
      context_overflow: '提供商报告上下文超限',
      manual: '手动压缩',
    }],
  ] as const)('localizes every known trigger in %s without blaming the summary model', (_language, catalog, labels) => {
    const localizedText: ClassicConversationRendererPorts['localizedText'] = (key, fallback, values) => (
      (catalog[key as keyof typeof catalog] ?? fallback).replace(/\{(\w+)\}/g,
        (match, name: string) => String(values?.[name] ?? match))
    );
    for (const [trigger, label] of Object.entries(labels)) {
      for (const status of ['running', 'succeeded', 'failed'] as const) {
        const {dom, node} = render({trigger, status, summaryModel: 'Luna'}, localizedText);
        try {
          expect(node.querySelector('.activity-event__body > .activity-event__facts')?.textContent)
            .toBe(`${catalog['activity.compaction.summaryModel'].replace('{model}', 'Luna')} · ${label}`);
          expect(node.querySelector('details')?.textContent).toContain(trigger);
          expect(node.querySelector('details')?.textContent)
            .toContain(catalog['activity.compaction.taskModel'].replace('{model}', 'Sol'));
        } finally { dom.window.close(); }
      }
    }
  });

  it.each([
    ['window', 'Context budget threshold reached'],
    ['dispatch_guard', 'Request too large before sending'],
    ['context_overflow', 'Provider context overflow'],
  ])('uses safe English fallback when a translator lacks %s', (trigger, label) => {
    const {dom, node} = render({trigger, summaryModel: 'Luna'}, (_key, fallback, values) => (
      fallback.replace(/\{(\w+)\}/g, (match, name: string) => String(values?.[name] ?? match))
    ));
    try {
      expect(node.querySelector('.activity-event__body > .activity-event__facts')?.textContent)
        .toBe(`Summary model: Luna · ${label}`);
    } finally { dom.window.close(); }
  });

  it.each([undefined, '', 'future_kind'])('does not invent a cause for a missing/unknown trigger %s', trigger => {
    const {dom, node} = render({trigger, summaryModel: 'Luna'});
    try {
      expect(node.querySelector('.activity-event__body > .activity-event__facts')?.textContent)
        .toBe('Summary model: Luna');
    } finally { dom.window.close(); }
  });

  it.each([
    {status: 'failed' as const, severity: 'error' as const},
    {status: 'running' as const, severity: 'info' as const},
    {status: 'aborted' as const, severity: 'warning' as const},
    {status: 'succeeded' as const, severity: 'warning' as const},
  ])('never folds attention reasons for $status/$severity', extra => {
    const {dom, node} = render({...extra, detail: 'Original failure/attention reason'});
    try {
      expect(node.querySelector('.activity-event__body > .activity-event__detail')?.textContent)
        .toBe('Original failure/attention reason');
    } finally { dom.window.close(); }
  });

  it('keeps reader disclosure and focus across updates without modifying the receipt', () => {
    const {dom, node, update} = render({summaryModel: 'Luna', trigger: 'working_set'});
    try {
      const details = node.querySelector<HTMLDetailsElement>('details')!;
      details.open = true;
      details.querySelector('summary')!.focus();
      update({durationMs: 41000});
      expect(node.querySelector<HTMLDetailsElement>('details')!.open).toBe(true);
      expect(dom.window.document.activeElement).toBe(node.querySelector('details > summary'));
      node.querySelector<HTMLDetailsElement>('details')!.open = false;
      update({durationMs: 42000});
      expect(node.querySelector<HTMLDetailsElement>('details')!.open).toBe(false);
    } finally { dom.window.close(); }
  });

  it('does not guess unknown trigger meanings or interpret diagnostic markup', () => {
    const {dom, node} = render({trigger: 'future_kind', summaryModel: '<img src=x>',
      detail: '<script>unsafe()</script>'});
    try {
      expect(node.querySelector('.activity-event__body > .activity-event__facts')?.textContent)
        .not.toContain('future kind');
      expect(node.querySelector('details')?.textContent).toContain('future_kind');
      expect(node.querySelector('img, script')).toBeNull();
      expect(node.querySelector('details')?.textContent).toContain('<script>unsafe()</script>');
    } finally { dom.window.close(); }
  });
});

describe('terminal error explanation and retained diagnostics', () => {
  it.each([['en', en], ['zh', zh]] as const)('localizes %s refusal and keeps complete evidence available', (_language, labels) => {
    const dom = new JSDOM('<!doctype html><body><div></div></body>');
    const node = dom.window.document.querySelector('div')!;
    const kind = 'execution_token_usage_unknown';
    const terminalError = {kind, severity: 'warning' as const, retryable: false,
      message: 'Wire fallback', hint: '', model: '', context: '', source: '', raw: '', titleKey: `err.k.${kind}.title`, hintKey: `err.k.${kind}.hint`,
      detail: '<script>bad()</script>' + 'd'.repeat(500) + ' COMPLETE_TAIL'};
    const block = {kind: 'activity-event', blockId: 'failure', identitySource: 'contract', source: {},
      terminalError, value: {id: 'failure', spanId: 'failure', seq: 1, occurredAt: 1000,
        kind: 'error', status: 'failed', severity: 'warning', summary: 'Turn failed', count: 1},
    } as Extract<ConversationBlockViewModel, {kind: 'activity-event'}>;
    const ports: ClassicConversationRendererPorts = {
      renderSafeMarkdownHtml: () => { throw new Error('Error content must be escaped'); },
      localizedText: (key, fallback) => (labels as Record<string, string>)[key] ?? fallback,
    };
    try {
      renderActivityEventBlock(node, block, ports);
      const card = node.querySelector('.error-block')!;
      expect(card.getAttribute('data-error-kind')).toBe(kind);
      expect(card.querySelector('.error-block-message')?.textContent).toBe(labels[`err.k.${kind}.title`]);
      expect(card.querySelector('.error-block-hint')?.textContent).toContain(labels[`err.k.${kind}.hint`]);
      expect(card.querySelector('button, script')).toBeNull();
      const evidence = node.querySelector<HTMLDetailsElement>('.activity-event__error-diagnostics')!;
      expect(evidence.open).toBe(false);
      expect(evidence.textContent).toContain('COMPLETE_TAIL');
      evidence.open = true;
      evidence.querySelector('summary')!.focus();
      block.terminalError!.detail += ' UPDATED';
      renderActivityEventBlock(node, block, ports);
      expect(node.querySelector<HTMLDetailsElement>('.activity-event__error-diagnostics')?.open).toBe(true);
      expect(node.querySelector('.activity-event__error-diagnostics summary')).toBe(dom.window.document.activeElement);
      expect(node.querySelector('.activity-event__terminal-envelope')?.textContent).toContain('COMPLETE_TAIL UPDATED');
      expect(node.querySelector('.activity-event__copy-diagnostics')).not.toBeNull();
    } finally { dom.window.close(); }
  });
});
