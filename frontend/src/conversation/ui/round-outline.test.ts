// @vitest-environment jsdom
import { expect, it } from 'vitest';
import { RoundOutline } from './round-outline';
import type { ConversationTurnViewModel, ToolBlockViewModel } from '../presentation/conversation-view-model';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createTurnState } from '../domain/turn-store';
import { createConversationSurface } from './conversation-surface';
import { createClassicConversationRenderers } from './classic-conversation-renderers';
import en from '../../i18n/locales/en.json';

const block = (status: string): ToolBlockViewModel => ({ kind: 'tool', blockId: 'failed',
  identitySource: 'contract', source: {}, toolCallId: 'failed', name: 'mcp__llm__experiment_eval_jobs',
  input: {}, result: { status, content: 'original raw evidence' },
  round: { toolName: 'mcp__llm__experiment_eval_jobs', status } });
// Only the assistant role participates in this presentation controller; it
// reads the Turn's ordered blocks to find large rounds.
const turnWith = (value: ToolBlockViewModel) => ({ role: 'assistant', blocks: [value] }) as unknown as ConversationTurnViewModel;

it('keeps concise generic failure diagnostics visible and raw evidence folded on first paint', () => {
  const node = document.createElement('div');
  node.innerHTML = '<details data-tool-failure-evidence="collapsed"><summary class="ptool-result-header">experiment has not run yet</summary><pre>raw envelope</pre></details>';
  const outline = new RoundOutline(node);
  const value = block('error');
  const turn = turnWith(value);
  outline.update(turn, [value]); outline.patchBlock(node, value); outline.completeBlock(node, value);
  const details = node.querySelector('details')!;
  expect(details.open).toBe(false);
  expect(node.dataset.actionState).toBe('attention');
  expect(node.querySelector('summary')?.textContent).toContain('experiment has not run yet');
  expect(node.querySelector('.conversation-action-state')?.textContent).toBe('Review details');
  for (const choice of [true, false]) {
    details.open = choice;
    outline.update(turn, [value]); outline.completeBlock(node, value);
    expect(details.open).toBe(choice);
  }
});

it('lets a swarm panel verdict chip stand as the only state in its header', () => {
  const node = document.createElement('div');
  node.innerHTML = '<div class="sw-panel"><div class="sw-header"><div class="sw-header-left">'
    + '<span class="sw-header-title">Parallel execution</span></div><div class="sw-header-right">'
    + '<span class="sw-status-pill sw-pill-running">Working…</span></div></div></div>';
  const outline = new RoundOutline(node);
  const value: ToolBlockViewModel = { ...block('running'), blockId: 'swarm', toolCallId: 'swarm',
    name: 'spawn_agents', round: { toolName: 'spawn_agents', status: 'searching', _swarmActive: true } };
  outline.update(turnWith(value), [value]); outline.patchBlock(node, value); outline.completeBlock(node, value);
  expect(node.dataset.actionState).toBe('active');
  expect(node.querySelector<HTMLElement>('.sw-header')?.dataset.actionSummary).toBe('true');
  expect(node.querySelector('.conversation-action-state')).toBeNull();
});

it('still opens structured actionable errors and active evidence on first paint', () => {
  for (const status of ['error', 'running']) {
    const node = document.createElement('div');
    node.innerHTML = '<details><summary class="ptool-result-header">Actionable evidence</summary></details>';
    const outline = new RoundOutline(node);
    const value = block(status);
    outline.update(turnWith(value), [value]); outline.completeBlock(node, value);
    expect(node.querySelector('details')?.open).toBe(true);
  }
});

/* The reported shape: one model response issued sibling execute_program calls.
 * The real view model, Surface and classic renderers run; only the Turn is synthetic. */
const scope = { taskId: 'task', attemptId: 'attempt' };
const source = (id: string) => `const doc = await tools.invoke('mcp__xuecheng__read_doc', {doc: '${id}'}); return doc;`;
const gatewaySegment = (id: string, llmRound: number, status: string | null) => ({
  ...scope, type: 'tool_use', blockId: `tool:${id}`, id, name: 'execute_program', llmRound,
  input: { code: source(id) },
  ...(status === null ? {} : { _round: { ...scope, toolCallId: id, toolName: 'execute_program',
    llmRound, status, toolArgs: { code: source(id) } } }),
});
const programRow = (id: string, llmRound: number, roundNum: number, fields: Record<string, unknown>) => ({
  ...scope, roundNum, llmRound, _programSynthetic: true, _programCallId: id,
  programSource: 'execute_program', programBackend: 'local_javascript', programCode: source(id),
  childCallIds: [], childToolNames: [], ...fields,
});
const handedBack = { programStatus: 'completed', status: 'done',
  programStats: { outcome: 'needs_model', tool_calls: 1, successful_tool_calls: 0 },
  programResult: { kind: 'observation', reason: 'The document read was refused (619).', data: { code: 619 } } };
const completed = { programStatus: 'completed', status: 'done',
  programStats: { outcome: 'completed', tool_calls: 1, successful_tool_calls: 1 },
  programResult: { title: 'Synthetic document' } };
const running = { programStatus: 'running', status: 'searching' };
const ids = Array.from({ length: 9 }, (_, index) => `call-${index + 1}`);
const child = (parent: string, n: number, status = 'done') => ({
  ...scope, type: 'tool_use', blockId: `tool:child-${n}`, id: `child-${n}`, name: 'mcp__xuecheng__read_doc',
  llmRound: 36, input: { doc: String(n) },
  _round: { ...scope, toolCallId: `child-${n}`, toolName: 'mcp__xuecheng__read_doc', llmRound: 36,
    roundNum: 8_700_000 + n, status, parentToolCallId: parent, attentionKind: 'routine',
    toolArgs: { doc: String(n) }, results: [{ text: 'Permission denied (619)' }] },
});
const translate = (key: string, n?: number) => String((en as Record<string, string>)[key] ?? key)
  .replace('{n}', String(n));
const ports = { renderSafeMarkdownHtml: (value: string) => `<p>${value}</p>`,
  localizedText: (_key: string, fallback: string, values?: Readonly<Record<string, string | number>>) =>
    fallback.replace(/\{(\w+)\}/g, (token, name: string) => String(values?.[name] ?? token)) };

function mountTurn(status: 'running' | 'completed', programs: ReadonlyArray<[string, Record<string, unknown>]>,
  pending: number, solo: Record<string, unknown> = handedBack) {
  const container = document.createElement('main'); document.body.append(container);
  const surface = createConversationSurface(container, {
    ...createClassicConversationRenderers(ports), roundOutlineLabel: translate,
  });
  const render = (turnStatus: typeof status, rows: typeof programs, queued: number, soloFields = solo) => {
    const state = createTurnState('conv');
    const started = new Set(rows.map(([id]) => id));
    state.turnsById.turn = {
      turnId: 'turn', presentationId: 'turn', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
      actor: 'assistant', kind: 'reply', status: turnStatus, currentAttemptId: null, projectionRevision: 1,
      createdAt: 1, updatedAt: 1, settlement: turnStatus === 'completed' ? { outcome: 'completed' } : {},
      projection: {
        segments: [
          { ...scope, type: 'text', blockId: 'narration', llmRound: 36, text: 'Reading the documents.' },
          ...ids.slice(0, rows.length + queued).map(id => gatewaySegment(id, 36,
            started.has(id) ? 'done' : 'searching')),
          child('call-1', 1), child('call-2', 2, 'error'),
          { ...scope, type: 'tool_use', blockId: 'tool:solo', id: 'solo', name: 'execute_program',
            llmRound: 37, input: { code: source('solo') }, _round: { ...scope, toolCallId: 'solo',
              toolName: 'execute_program', llmRound: 37, status: 'done', toolArgs: { code: source('solo') } } },
        ],
        toolRounds: [
          ...rows.map(([id, fields], index) => programRow(id, 36, 36_000_001 + index, fields)),
          programRow('solo', 37, 37_000_001, soloFields),
        ],
      },
    } as never;
    state.laneOrder.main = ['turn'];
    surface.render(selectConversationViewModel(state));
  };
  render(status, programs, pending);
  return { container, surface, render };
}

const heads = (container: HTMLElement) => [...container.querySelectorAll<HTMLButtonElement>(
  '.conversation-program-batch')];
const blockNode = (container: HTMLElement, id: string) => [...container.querySelectorAll<HTMLElement>(
  '[data-block-id]')].find(node => node.dataset.blockId === id)!;
const folded = (container: HTMLElement) => [...container.querySelectorAll<HTMLElement>(
  '[data-program-batch-folded="true"]')].map(node => node.dataset.blockId);
const memberIds = [...ids.map(id => `program:${id}`), 'tool:child-1', 'tool:child-2'];

it('folds a model round of sibling programs under one batch head; a lone program keeps its card', () => {
  const { container, surface, render } = mountTurn('completed',
    ids.map((id, index) => [id, index ? handedBack : completed]), 0);
  try {
    const [head] = heads(container);
    expect(heads(container)).toHaveLength(1);
    expect(head.textContent).toBe('Programs: 9 · 1 completed · 8 returned for model judgment');
    expect(head.getAttribute('aria-expanded')).toBe('false');
    expect(head.nextElementSibling).toBe(blockNode(container, 'program:call-1'));
    // The head travels with its first member; the layout alone places prose.
    const narration = blockNode(container, 'narration');
    expect(narration.compareDocumentPosition(head))
      .toBe(narration.compareDocumentPosition(blockNode(container, 'program:call-1')));
    expect(folded(container)).toEqual(memberIds);
    // A handoff returns to the model: member cards never open themselves.
    for (const id of ids) {
      expect(blockNode(container, `program:${id}`).querySelector<HTMLDetailsElement>('.ptc-card')!.open).toBe(false);
    }
    // The single-program round is unchanged: no head, its handoff card opens.
    const solo = blockNode(container, 'program:solo');
    expect(solo.dataset.programBatch).toBeUndefined();
    expect(solo.querySelector<HTMLDetailsElement>('.ptc-card')!.open).toBe(true);

    head.click();
    expect(head.getAttribute('aria-expanded')).toBe('true');
    expect(folded(container)).toEqual([]);
    expect(ids.every(id => !blockNode(container, `program:${id}`)
      .querySelector<HTMLDetailsElement>('.ptc-card')!.open)).toBe(true);
    // A child's refused read is its program's evidence, already returned to the
    // model: the row keeps its failure state but does not open its evidence.
    const failedChild = blockNode(container, 'tool:child-2');
    expect(failedChild.dataset.actionState).toBe('attention');
    expect(failedChild.querySelector<HTMLDetailsElement>('details')?.open).toBe(false);
    // A replayed projection keeps the reader's choice and the retained head.
    render('completed', ids.map((id, index) => [id, index ? handedBack : completed]), 0);
    expect(heads(container)).toEqual([head]);
    expect(head.getAttribute('aria-expanded')).toBe('true');
    expect(folded(container)).toEqual([]);
    head.click();
    expect(folded(container)).toEqual(memberIds);
  } finally { surface.dispose(); container.remove(); }
});

const cards = (container: HTMLElement, batched = true) => [...container.querySelectorAll<HTMLElement>(
  '[data-block-id]')].filter(node => (node.dataset.programBatch !== undefined) === batched)
  .map(node => node.querySelector<HTMLDetailsElement>('details.ptc-card')).filter(card => card !== null);

it('keeps queued and running programs closed unless the reader opens one', () => {
  const { container, surface, render } = mountTurn('running', [['call-1', running]], 3, running);
  try {
    // One started program and three queued gateway calls (`searching`).
    expect(cards(container)).toHaveLength(4);
    expect(cards(container).map(card => card.open)).toEqual([false, false, false, false]);
    heads(container)[0].click();
    expect(cards(container).map(card => card.open)).toEqual([false, false, false, false]);
    // The reader opens the running card; it keeps that choice as it hands back.
    const [opened] = cards(container);
    opened.open = true;
    render('running', [['call-1', handedBack], ['call-2', running]], 2);
    expect(cards(container).filter(card => card.open)).toEqual([opened]);

    // A lone program keeps its reveal: open from first paint while it runs,
    // still open once it hands back.
    render('running', ids.slice(0, 4).map(id => [id, handedBack]), 0);
    const [solo] = cards(container, false);
    expect(solo.open).toBe(true);
    expect(cards(container).filter(card => card.open)).toEqual([opened]);
    render('completed', ids.slice(0, 4).map(id => [id, handedBack]), 0, handedBack);
    expect(cards(container, false)).toEqual([solo]);
    expect(solo.open).toBe(true);
    expect(cards(container).filter(card => card.open)).toEqual([opened]);
  } finally { surface.dispose(); container.remove(); }
});

it('folds the first program a live round announced alone once its siblings join', () => {
  const { container, surface, render } = mountTurn('running', [], 1);
  try {
    // Announced alone, the call is a lone program and reveals itself as before.
    expect(heads(container)).toEqual([]);
    const [first] = cards(container, false).filter(card => card.closest<HTMLElement>(
      '[data-block-id]')!.dataset.blockId === 'tool:call-1');
    expect(first.open).toBe(true);
    render('running', [], 9);
    const [head] = heads(container);
    expect(head.textContent).toBe('Programs: 9 · 9 waiting');
    expect(cards(container)).toHaveLength(9);
    expect(cards(container)[0]).toBe(first);
    head.click();
    expect(cards(container).map(card => card.open)).toEqual(Array(9).fill(false));
  } finally { surface.dispose(); container.remove(); }
});

it('counts live programs as they run and keeps the reader choice through settlement', () => {
  const { container, surface, render } = mountTurn('running', [['call-1', running]], 8);
  try {
    const [head] = heads(container);
    expect(head.textContent).toBe('Programs: 9 · 1 running · 8 waiting');
    expect(head.getAttribute('aria-expanded')).toBe('false');
    head.click();
    render('running', [['call-1', handedBack], ['call-2', running]], 7);
    expect(heads(container)).toEqual([head]);
    expect(head.textContent).toBe('Programs: 9 · 1 running · 7 waiting · 1 returned for model judgment');
    expect(head.getAttribute('aria-expanded')).toBe('true');
    // Becoming a handoff never re-opens a member card.
    expect(blockNode(container, 'program:call-1').querySelector<HTMLDetailsElement>('.ptc-card')!.open).toBe(false);
    render('completed', ids.map((id, index) => [id, index === 1 ? completed : handedBack]), 0);
    expect(heads(container)).toEqual([head]);
    expect(head.textContent).toBe('Programs: 9 · 1 completed · 8 returned for model judgment');
    expect(head.getAttribute('aria-expanded')).toBe('true');
    expect(folded(container)).toEqual([]);
  } finally { surface.dispose(); container.remove(); }
});
