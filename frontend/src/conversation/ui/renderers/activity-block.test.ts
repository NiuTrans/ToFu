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

describe('clickable compact compression receipts', () => {
  it('puts the recorded model beside the status and opens the receipt without a Details label', () => {
    const { dom, node } = render({ summaryModel: 'Luna', summarySelectedModel: 'chosen-Luna',
      taskModel: 'Sol', tokensBefore: 100000, tokensAfter: 15000, trigger: 'working_set' });
    try {
      const details = node.querySelector<HTMLDetailsElement>('details.activity-event--compaction')!;
      expect(details).not.toBeNull();
      expect(details.open).toBe(false);
      const summary = details.querySelector(':scope > summary')!;
      expect(summary.textContent).toContain('Context compaction');
      const model = summary.querySelector('.activity-event__compaction-model')!;
      expect(model.textContent).toBe('Luna');
      expect(model.getAttribute('title')).toContain('Summary model: Luna');
      expect(summary.textContent).not.toContain('Details');
      expect(summary.textContent).not.toContain('chosen-Luna');
      expect(model.parentElement).toBe(summary.querySelector('.activity-event__line'));
      // The snapshot action is inline in the header, not behind the fold.
      const open = summary.querySelector('[data-conversation-action="open-compaction"]')!;
      expect(open.getAttribute('data-operation')).toBe('archive');
      expect(details.querySelector('.activity-event__compaction-content')?.textContent)
        .toContain('Selected summary model: chosen-Luna');
    } finally { dom.window.close(); }
  });
});

describe('compaction summary model provenance', () => {
  it('shows summary execution while keeping unchanged main-model diagnostics folded', () => {
    const {dom, node} = render({taskModel: 'Sol', summaryModel: 'Luna',
      summarySelectedModel: 'Luna', summaryProviderId: 'summary-provider'});
    try {
      expect(node.querySelector('.activity-event__compaction-model')?.textContent).toBe('Luna');
      // Routing ids stay in the fold, never in the visible header.
      const header = node.querySelector('.activity-event__compaction-summary')!;
      expect(header.textContent).not.toContain('Sol');
      expect(header.textContent).not.toContain('summary-provider');
      expect(header.textContent).not.toContain('Main model');
      expect(node.querySelector('.activity-event__compaction-content')?.textContent)
        .toContain('summary-provider');

      expect(node.querySelector<HTMLDetailsElement>('details')?.open).toBe(false);
      expect(node.querySelector('button')?.dataset.operation).toBe('archive');
    } finally { dom.window.close(); }
  });

  it('does not reinterpret historical parent model/provider as the summary', () => {
    const {dom, node} = render({providerId: 'parent-provider'});
    try {
      expect(node.querySelector('.activity-event__compaction-model')?.textContent).toBe('');
      expect(node.querySelector('.activity-event__compaction-model')?.getAttribute('title'))
        .toContain('Summary model not recorded');
      expect(node.textContent).not.toContain('Summary model: Sol');
      expect(node.textContent).not.toContain('parent-provider');
    } finally { dom.window.close(); }
  });

  it('labels selection-only evidence honestly, including failed summaries', () => {
    const {dom, node} = render({status: 'failed', summarySelectedModel: 'chosen-Luna'});
    try {
      expect(node.querySelector('.activity-event__compaction-model')?.getAttribute('title'))
        .toContain('Summary model not recorded');
      expect(node.querySelector('.activity-event__compaction-content')?.textContent)
        .toContain('Selected summary model: chosen-Luna');
      expect(node.querySelector('.activity-event__compaction-summary')?.textContent)
        .not.toContain('chosen-Luna');
    } finally { dom.window.close(); }
  });
});


it('shows distinct recorded retry models rather than a false single-model attribution', () => {
  const {dom, node} = render({status: 'failed', summaryCalls: [
    {model: 'Luna', providerId: 'one', status: 'failed'},
    {model: 'different-Luna', providerId: 'two', status: 'failed'},
  ]});
  try {
    expect(node.querySelector('.activity-event__compaction-model')?.textContent)
      .toBe('Luna · different-Luna');
    expect(node.querySelector('.activity-event__compaction-model')?.getAttribute('title'))
      .not.toContain('Summary model not recorded');
  } finally { dom.window.close(); }
});


describe('compact compression diagnostics', () => {
  it.each([
    ['working_set', 'Long conversation'],
    ['window', 'Context limit reached'],
    ['dispatch_guard', 'Message too large to send'],
    ['context_overflow', 'Provider reported context overflow'],
    ['reactive', 'Provider rejected the context size'],
    ['manual', 'Manual compression'],
    ['force', 'Requested compaction'],
    ['checkpoint', 'Saved on interruption'],
  ])('uses a short label for %s, retaining the original reason', (trigger, label) => {
    const {dom, node} = render({trigger,
      detail: 'estimated input crossed 244,800-token working_set threshold'});
    try {
      expect(node.querySelector('.activity-event__compaction-trigger')?.textContent).toBe(label);
      expect(node.querySelector('.activity-event__compaction-accounting > .activity-event__facts')).toBeNull();
      expect(node.querySelector('.activity-event__body > .activity-event__detail')).toBeNull();
      expect(node.querySelector('details .activity-event__detail')?.textContent)
        .toBe('estimated input crossed 244,800-token working_set threshold');
    } finally { dom.window.close(); }
  });

  it.each([
    ['en', en, {
      working_set: 'Long conversation',
      window: 'Context limit reached',
      dispatch_guard: 'Message too large to send',
      context_overflow: 'Provider reported context overflow',
      reactive: 'Provider rejected the context size',
      manual: 'Manual compression',
      force: 'Requested compaction',
      checkpoint: 'Saved on interruption',
    }],
    ['zh', zh, {
      working_set: '对话内容较多',
      window: '接近上下文上限',
      dispatch_guard: '发送内容过大',
      context_overflow: '模型报告上下文超限',
      reactive: '模型拒绝了过长的上下文',
      manual: '手动压缩',
      force: '按要求压缩',
      checkpoint: '中断时保存的上下文',
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
          expect(node.querySelector('.activity-event__compaction-model')?.textContent).toBe('Luna');
          expect(node.querySelector('.activity-event__compaction-trigger')?.textContent).toBe(label);
          expect(node.querySelector('details')?.textContent).toContain(trigger);
          // The main model belongs to the turn header, not the receipt.
          expect(node.querySelector('.activity-event__compaction-summary')?.textContent)
            .not.toContain(catalog['activity.compaction.taskModel'].replace('{model}', 'Sol'));
        } finally { dom.window.close(); }
      }
    }
  });

  it.each([
    ['window', 'Context limit reached'],
    ['dispatch_guard', 'Message too large to send'],
    ['context_overflow', 'Provider reported context overflow'],
    ['reactive', 'Provider rejected the context size'],
    ['force', 'Requested compaction'],
    ['checkpoint', 'Saved on interruption'],
  ])('uses safe English fallback when a translator lacks %s', (trigger, label) => {
    const {dom, node} = render({trigger, summaryModel: 'Luna'}, (_key, fallback, values) => (
      fallback.replace(/\{(\w+)\}/g, (match, name: string) => String(values?.[name] ?? match))
    ));
    try {
      expect(node.querySelector('.activity-event__compaction-model')?.textContent).toBe('Luna');
      expect(node.querySelector('.activity-event__compaction-trigger')?.textContent).toBe(label);
    } finally { dom.window.close(); }
  });

  it.each([undefined, '', 'future_kind'])('does not invent a cause for a missing/unknown trigger %s', trigger => {
    const {dom, node} = render({trigger, summaryModel: 'Luna'});
    try {
      expect(node.querySelector('.activity-event__compaction-model')?.textContent).toBe('Luna');
      expect(node.querySelector('.activity-event__compaction-trigger')).toBeNull();
      expect(node.querySelector('.activity-event__compaction-accounting > .activity-event__facts'))
        .toBeNull();
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
      expect(node.querySelector('.activity-event__compaction-model')?.textContent)
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

describe('activity fact line is not a second copy of the summary', () => {
  const localized = (key: string, fallback: string,
    values?: Record<string, unknown>) => ((zh as Record<string, string>)[key] || fallback)
    .replace(/\{(\w+)\}/g, (match, name) => String(values?.[name] ?? match));

  function row(extra: Partial<TurnActivityEntry>) {
    const dom = new JSDOM('<!doctype html><body><div></div></body>');
    const node = dom.window.document.querySelector('div')!;
    const entry: TurnActivityEntry = {
      id: 'row', spanId: 'row', seq: 1, occurredAt: 1000, kind: 'model',
      status: 'failed', severity: 'error', count: 1, ...extra,
    };
    renderActivityEventBlock(node, {
      kind: 'activity-event', blockId: entry.id, identitySource: 'contract',
      value: entry, source: entry,
    } as Extract<ConversationBlockViewModel, { kind: 'activity-event' }>, {
      renderSafeMarkdownHtml: () => { throw new Error('plain text only'); },
      localizedText: localized,
    } as ClassicConversationRendererPorts);
    return { dom, node };
  }

  it('names the model once on a failed request, keeping provider and key', () => {
    const { dom, node } = row({
      model: 'claude-opus-5.5', providerId: 'example-corp', keyHint: '2031****4861',
      summaryKey: 'activity.model.requestFailed',
      summaryArgs: { model: 'claude-opus-5.5' }, reasonCode: 'ModelRefusalError',
    });
    try {
      expect(node.querySelector('.activity-event__summary')!.textContent)
        .toBe('claude-opus-5.5 请求失败');
      expect(node.querySelector('.activity-event__facts')!.textContent)
        .toBe('example-corp · 2031****4861 · ModelRefusalError');
      expect(node.textContent!.split('claude-opus-5.5')).toHaveLength(2);
    } finally { dom.window.close(); }
  });

  it('leads with the classified cause, keeping the raw class in the tooltip', () => {
    const { dom, node } = row({
      model: 'claude-opus-5.5', providerId: 'example-corp', statusCode: 503,
      summaryKey: 'activity.model.requestFailed',
      summaryArgs: { model: 'claude-opus-5.5' },
      reasonCode: 'RateLimitError', envelopeKind: 'upstream_error',
    });
    try {
      const facts = node.querySelector('.activity-event__facts')!;
      expect(facts.textContent).toBe('上游故障 · example-corp · HTTP 503');
      expect(facts.textContent).not.toContain('RateLimitError');
      // The routing class stays reachable without contradicting the cause.
      expect(facts.querySelector<HTMLElement>('.activity-event__cause')!.title)
        .toBe('RateLimitError');
    } finally { dom.window.close(); }
  });

  it('reads the cause, not the routing class, on a gateway 503 attempt row', () => {
    // The exact fields a failed provider attempt carries (no errorDetail),
    // as reported for kunlunbedrock.claude-opus-5.5-m17.
    const { dom, node } = row({
      requestLevel: 'provider_attempt', attempt: 1, attemptScope: 'dispatcher',
      model: 'kunlunbedrock.claude-opus-5.5-m17', providerId: 'example-corp',
      keyHint: '2080********6672', statusCode: 503,
      summaryKey: 'activity.model.requestFailed',
      summaryArgs: { model: 'kunlunbedrock.claude-opus-5.5-m17' },
      reasonCode: 'RateLimitError', envelopeKind: 'upstream_error',
    });
    try {
      const facts = node.querySelector('.activity-event__facts')!;
      expect(facts.textContent)
        .toBe('上游故障 · example-corp · 2080********6672 · HTTP 503');
      expect(node.textContent).not.toContain('RateLimitError');
      expect(facts.querySelector<HTMLElement>('.activity-event__cause')!.title)
        .toBe('RateLimitError');
    } finally { dom.window.close(); }
  });

  it('keeps a wire model that the summary does not name', () => {
    const { dom, node } = row({
      model: 'kunlunbedrock.claude-opus-5.5-m17', providerId: 'example-corp',
      summaryKey: 'activity.model.requestFailed',
      summaryArgs: { model: 'claude-opus-5.5' },
    });
    try {
      expect(node.querySelector('.activity-event__facts')!.textContent)
        .toContain('kunlunbedrock.claude-opus-5.5-m17');
    } finally { dom.window.close(); }
  });

  it('drops the raw phase enum and switch taxonomy from the facts line', () => {
    const phase = row({
      kind: 'status', status: 'waiting', severity: 'warning', phase: 'retrying',
      reasonCode: 'retrying', model: 'kimi-k3.1-pro',
      summaryKey: 'stream.phase.refusalSwitchback',
      summaryArgs: { model: 'claude-opus-5.5', fallback: 'kimi-k3.1-pro' },
    });
    try {
      expect(phase.node.textContent).not.toContain('retrying');
      expect(phase.node.querySelector('.activity-event__facts')).toBeNull();
    } finally { phase.dom.window.close(); }

    const switched = row({
      status: 'switched', severity: 'warning', reasonCode: 'content_filter',
      summaryKey: 'activity.model.switched',
      summaryArgs: { from: 'claude-opus-5.5', to: 'kimi-k3.1-pro' },
      detail: 'user sensitive words detected: test_sensitive',
    });
    try {
      expect(switched.node.textContent).not.toContain('content_filter');
      expect(switched.node.querySelector('.activity-event__detail')!.textContent)
        .toBe('user sensitive words detected: test_sensitive');
    } finally { switched.dom.window.close(); }
  });

  it('keeps a typed HTTP reason and the status code as real evidence', () => {
    const { dom, node } = row({
      kind: 'status', status: 'waiting', severity: 'warning', phase: 'retrying',
      reasonCode: 'stream.retryReason.rateLimited', statusCode: 429,
      summary: 'Rate limited; waiting',
    });
    try {
      const facts = node.querySelector('.activity-event__facts')!.textContent!;
      expect(facts).toContain('HTTP 429');
      expect(facts).toContain('请求被限流 (429)');
    } finally { dom.window.close(); }
  });
});
