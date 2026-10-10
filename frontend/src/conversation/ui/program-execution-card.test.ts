// @vitest-environment jsdom
/** Cross-surface behavior and reader-owned disclosure, including transitions. */
import { describe, expect, it } from 'vitest';
import { renderDefaultBlock } from './surface-default-renderers';
import { renderProgramBlock, renderToolBlock } from './renderers/tool-blocks';
import { programExecutionCardHtml } from './program-execution-card';
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../presentation/conversation-view-model';

function block(status = 'completed'): Extract<ConversationBlockViewModel, { kind: 'program' }> {
  return { kind: 'program', blockId: 'program', round: {
    programStatus: status, programBackend: 'local_javascript',
    programCode: 'const rows = await tools.invoke("read", {}); return rows;',
    programResult: { kind: 'observation', reason: 'Choose <script>unsafe()</script>', data: { rows: ['<img src=x onerror=alert(1)>'] } },
    childCallIds: ['child'], childToolNames: ['read'],
    programStats: { outcome: 'needs_model', result_delivery: { outputBytes: 0 }, result_bytes: 250, call_stages: 1, parallel_stages: 0, result_reads: 3 },
  } } as unknown as Extract<ConversationBlockViewModel, { kind: 'program' }>;
}

const surfaces = {
  default: (node: HTMLElement, value: ReturnType<typeof block>) => renderDefaultBlock(node, value),
  classic: (node: HTMLElement, value: ReturnType<typeof block>) => renderProgramBlock(node, value, {} as ConversationTurnViewModel, {
    renderSafeMarkdownHtml: () => { throw new Error('Program evidence is not Markdown'); },
  }),
};

describe.each(Object.entries(surfaces))('%s program surface', (_name, render) => {
  it('shows reason and complete evidence ahead of separate collapsed JavaScript', () => {
    const node = document.createElement('div'); const value = block(); const before = JSON.stringify(value);
    render(node, value);
    expect(node.querySelector('.ptc-observation')?.textContent).toBe('Choose <script>unsafe()</script>');
    const result = node.querySelector<HTMLDetailsElement>('.ptc-result')!;
    const code = node.querySelector<HTMLDetailsElement>('.ptc-code')!;
    expect(result.open).toBe(true); expect(code.open).toBe(false);
    expect(JSON.parse(result.querySelector('pre')!.textContent!)).toEqual({ rows: ['<img src=x onerror=alert(1)>'] });
    expect(result.compareDocumentPosition(code) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(code.querySelector('code')?.className).toBe('language-javascript');
    expect(node.textContent).not.toContain('local_javascript');
    expect(node.querySelector('script,img')).toBeNull();
    expect(node.querySelector('.ptc-measurements')?.textContent).toContain('Tool output delivered to program: 0 B (0 bytes)');
    expect(node.querySelector('.ptc-measurements')?.textContent).toContain('Program return value: 250 B (250 bytes)');
    expect(node.querySelector<HTMLDetailsElement>('.ptc-technical')?.open).toBe(false);
    expect(JSON.stringify(value)).toBe(before);
  });

  it.each([
    ['null', null, 'null'],
    ['empty string', '', '""'],
    ['zero', 0, '0'],
    ['false', false, 'false'],
  ] as const)('keeps a completed %s result visible as aggregate and judgment evidence', (_name, result, expected) => {
    const node = document.createElement('div'); const value = block();
    for (const programResult of [result, { kind: 'observation', reason: 'Review this result', data: result }]) {
      render(node, { ...value, round: { ...value.round, programResult } });
      expect(node.querySelector('.ptc-result pre')?.textContent).toBe(expected);
      expect(node.querySelector('.ptc-result summary')).not.toBeNull();
    }
  });

  it('distinguishes absent output and waiting placeholders from settled empty values', () => {
    const node = document.createElement('div'); const value = block();
    render(node, { ...value, round: { ...value.round, programResult: undefined } });
    expect(node.querySelector('.ptc-result')).toBeNull();
    for (const programResult of [null, '']) {
      render(node, { ...value, round: { ...value.round, programStatus: 'running', programResult } });
      expect(node.querySelector('.ptc-result')).toBeNull();
    }
  });

  it('preserves independent folds, focused summary and DOM identity after evidence updates', () => {
    const node = document.createElement('div'); document.body.appendChild(node);
    try {
      const value = block(); render(node, value);
      const result = node.querySelector<HTMLDetailsElement>('.ptc-result')!;
      const code = node.querySelector<HTMLDetailsElement>('.ptc-code')!;
      result.open = false; code.open = true;
      const summary = code.querySelector('summary')!; summary.focus();
      render(node, { ...value, round: { ...value.round, programResult: { kind: 'observation', reason: 'Review the new evidence', data: { rows: ['updated'] } } } });
      expect(node.querySelector('.ptc-result')).toBe(result);
      expect(node.querySelector('.ptc-code')).toBe(code);
      expect(result.open).toBe(false); expect(code.open).toBe(true);
      expect(document.activeElement).toBe(summary);
      expect(node.querySelector('.ptc-observation')?.textContent).toBe('Review the new evidence');
    } finally { node.remove(); }
  });

  it('opens new handoff evidence at settlement while retaining a reader-opened source', () => {
    const node = document.createElement('div'); render(node, block('running'));
    const code = node.querySelector<HTMLDetailsElement>('.ptc-code')!; code.open = true;
    expect(node.querySelector('.ptc-needs-model')).toBeNull();
    render(node, block());
    expect(node.querySelector<HTMLDetailsElement>('.ptc-result')?.open).toBe(true);
    expect(node.querySelector('.ptc-code')).toBe(code); expect(code.open).toBe(true);
  });
});

it('renders native JavaScript and legacy absent measurements without estimates', () => {
  const node = document.createElement('div');
  node.innerHTML = programExecutionCardHtml({ programStatus: 'completed', programBackend: 'native_openai', programCode: 'return rows', programResult: { answer: 'done' } });
  expect(node.querySelector('.ptc-code code')?.className).toBe('language-javascript');
  expect(node.querySelector<HTMLDetailsElement>('.ptc-card')?.open).toBe(false);
  expect(node.querySelector<HTMLDetailsElement>('.ptc-result')?.open).toBe(false);
  expect(node.querySelector<HTMLDetailsElement>('.ptc-code')?.open).toBe(false);
  expect(node.querySelector('.ptc-measurements,.ptc-observation')).toBeNull();
  expect(node.textContent).not.toMatch(/native_openai|saved|saving/i);
});

it('escapes localized labels and ignores inherited reason keys', () => {
  const node = document.createElement('div');
  node.innerHTML = programExecutionCardHtml({ ...block().round, programResult: { kind: 'observation', reason: 'constructor', data: [] } }, (_key, fallback) => `<img src=x>${fallback}`);
  expect(node.querySelector('img')).toBeNull();
  expect(node.querySelector('.ptc-observation')?.textContent).toBe('constructor');
});


it('explains missing program results as a handoff instead of successful null', () => {
  const node = document.createElement('div');
  node.innerHTML = programExecutionCardHtml({
    programStatus: 'completed', programStats: { outcome: 'needs_model', tool_calls: 1 },
    programResult: { kind: 'observation', reason: 'missing_program_result', data: { completedToolCalls: 1 } },
  });
  expect(node.querySelector('.ptc-needs-model')?.textContent).toContain('Returned for model judgment');
  expect(node.querySelector('.ptc-observation')?.textContent).toContain('completed tool calls must not be repeated');
  expect(node.querySelector('.ptc-result pre')?.textContent).toContain('completedToolCalls');
});


describe('program call outcome counts', () => {
  it('shows zero successful calls alongside the rejected attempt', () => {
    const html = programExecutionCardHtml({ programStatus: 'completed',
      programStats: { outcome: 'needs_model', tool_calls: 1, successful_tool_calls: 0 },
      programResult: { kind: 'observation', reason: 'tool_not_done', data: { error: { code: 'tool_not_enabled' } } } });
    expect(html).toContain('0 successful tool calls · 1 attempts');
    expect(html).toContain('check its full name and availability');
  });
  it('does not invent success for old or contradictory records', () => {
    for (const stats of [{ tool_calls: 1 }, { tool_calls: 1, successful_tool_calls: 2 }]) {
      expect(programExecutionCardHtml({ programStatus: 'completed', programStats: stats })).not.toContain('successful tool calls');
    }
  });
});


describe.each(Object.entries(surfaces))('%s compact program disclosure', (_name, render) => {
  it('keeps zero-new-call reuse quiet with all counters and results inspectable', () => {
    const node = document.createElement('div');
    render(node, { ...block(), round: { programStatus: 'completed', programResult: { answer: 42 },
      childToolNames: ['read'], programStats: { tool_calls: 0, successful_tool_calls: 0,
        result_reads: 2, result_bytes: 2048, result_delivery: { outputBytes: 1048576 }, extra_counter: 7 } } });
    expect(node.querySelector('.ptc-head')?.textContent).toContain('No new tool calls · 2 retained results reused');
    expect(node.querySelector('.ptc-head')?.textContent).not.toMatch(/successful|bytes|child tool/);
    expect(node.querySelector<HTMLDetailsElement>('.ptc-card')?.open).toBe(false);
    expect(node.querySelector<HTMLDetailsElement>('.ptc-result')?.open).toBe(false);
    expect(node.querySelector<HTMLDetailsElement>('.ptc-technical')?.open).toBe(false);
    expect(node.querySelector('.ptc-measurements')?.textContent).toContain('1 MiB (1,048,576 bytes)');
    expect(node.querySelector('.ptc-measurements')?.textContent).toContain('2 KiB (2,048 bytes)');
    expect(node.querySelector('[data-rich-key="counters"] pre')?.textContent).toContain('extra_counter');
  });

  it('does not confuse unique tool names with calls in older records', () => {
    const node = document.createElement('div');
    render(node, { ...block(), round: { programStatus: 'completed', childToolNames: ['read', 'read'] } });
    expect(node.querySelector('.ptc-head')?.textContent).not.toMatch(/call|attempt/);
  });

  it.each(['error', 'completed'])('reveals new %s attention but retains dismissed attention and focused source', status => {
    const node = document.createElement('div'); document.body.append(node);
    try {
      render(node, block('running'));
      const shell = node.querySelector<HTMLDetailsElement>('.ptc-card')!;
      shell.open = true;
      const source = node.querySelector<HTMLDetailsElement>('.ptc-code')!;
      source.open = true; const summary = source.querySelector('summary')!; summary.focus();
      shell.open = false;
      render(node, block(status));
      expect(node.querySelector('.ptc-card')).toBe(shell); expect(shell.open).toBe(true);
      expect(node.querySelector('.ptc-code')).toBe(source); expect(source.open).toBe(true);
      expect(document.activeElement).toBe(summary);
      shell.open = false; render(node, block(status)); expect(shell.open).toBe(false);
    } finally { node.remove(); }
  });
});


describe.each(['default', 'classic-fallback', 'classic-rich'])('%s execution child attributes', surface => {
  it('marks only explicit execution parents and clears stale attrs on reused tool nodes', () => {
    const node = document.createElement('div');
    const render = (round: Record<string, unknown>) => {
      const value = { kind: 'tool', blockId: 'child', name: 'read_files', input: {}, round } as unknown as Extract<ConversationBlockViewModel, { kind: 'tool' }>;
      if (surface === 'default') renderDefaultBlock(node, value);
      else renderToolBlock(node, value, {} as ConversationTurnViewModel, {
        renderToolBlockHtml: surface === 'classic-rich' ? () => '<div class="rich-receipt">receipt</div>' : undefined,
      } as Parameters<typeof renderToolBlock>[3]);
    };
    for (const round of [{ parentToolCallId: 'program' }, { _programCallId: 'program' }]) {
      render(round);
      expect(node.classList.contains('ptool-parent-child')).toBe(true);
      expect(node.dataset.parentToolCallId).toBe('program');
      for (const unrelated of [{ _artifactOrigin: { parentToolCallId: 'program' } },
        { _programSynthetic: true, _programCallId: 'program' }, {}]) {
        render(unrelated);
        expect(node.classList.contains('ptool-parent-child')).toBe(false);
        expect(node.hasAttribute('data-parent-tool-call-id')).toBe(false);
      }
    }
  });
});
