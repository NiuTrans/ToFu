import { expect, it } from 'vitest';
import {
  programBatchLabelParts,
  programBatchStatus,
  summarizeProgramBatch,
} from './program-batch-presentation';

const program = (fields: Record<string, unknown>) => ({
  _programSynthetic: true, _programCallId: 'p', llmRound: 36,
  programBackend: 'local_javascript', programCode: 'return 1', ...fields,
});
const handoff = program({ programStatus: 'completed', status: 'done',
  programStats: { outcome: 'needs_model' },
  programResult: { kind: 'observation', reason: 'The read was refused; decide how to continue.' } });
const completed = program({ programStatus: 'completed', status: 'done', programStats: { outcome: 'completed' } });
const failed = program({ programStatus: 'error', status: 'error' });
const running = program({ programStatus: 'running', status: 'searching' });
const gateway = (status?: string) => ({ toolName: 'execute_program', toolArgs: { code: 'return 1' },
  ...(status ? { status } : {}) });
const child = (status: string, fields: Record<string, unknown> = {}) => ({
  toolName: 'mcp__xuecheng__read_doc', parentToolCallId: 'p', attentionKind: 'routine', status, ...fields });

it('states each program as its card does, with the owning lifecycle deciding waiting calls', () => {
  expect(programBatchStatus(handoff, false)).toBe('handoff');
  expect(programBatchStatus(completed, false)).toBe('completed');
  expect(programBatchStatus(failed, false)).toBe('failed');
  expect(programBatchStatus(running, true)).toBe('running');
  // A queued gateway call has no program run yet; settled, it was never answered.
  expect(programBatchStatus(gateway('pending'), true)).toBe('waiting');
  expect(programBatchStatus(gateway('searching'), false)).toBe('incomplete');
  expect(programBatchStatus(running, false)).toBe('incomplete');
  // Gateway `done` is Returned, never whole-program completion or a model handoff.
  expect(programBatchStatus(gateway('done'), false)).toBe('returned');
  expect(programBatchStatus(gateway('error'), false)).toBe('failed');
  // Without a known lifecycle the summary repeats the card: queued, not running.
  expect(programBatchStatus(gateway('searching'))).toBe('waiting');
});

it('folds two or more sibling programs whose outcomes return to the model', () => {
  expect(summarizeProgramBatch([handoff], false)).toBeNull();
  // The reported round: one result and eight handoffs whose child reads failed.
  const children = [...Array(7).fill(child('error')), child('done'), child('done')];
  const settled = summarizeProgramBatch([completed, ...Array(8).fill(handoff)], false,
    [completed, ...Array(8).fill(handoff), ...children])!;
  expect(settled).toMatchObject({ programCount: 9, attention: 'important', defaultCollapsed: true });
  expect(settled.statuses).toEqual([{ status: 'completed', count: 1 }, { status: 'handoff', count: 8 }]);
  expect(programBatchLabelParts(settled)).toEqual([
    { key: 'roundOutline.programs', count: 9 },
    { key: 'roundOutline.programsCompleted', count: 1, status: 'completed' },
    { key: 'roundOutline.programsHandoff', count: 8, status: 'handoff' },
  ]);

  const live = summarizeProgramBatch([running, ...Array(8).fill(gateway('pending'))], true)!;
  expect(live.statuses).toEqual([{ status: 'running', count: 1 }, { status: 'waiting', count: 8 }]);
  expect(live).toMatchObject({ attention: 'active', defaultCollapsed: true });
  expect(summarizeProgramBatch([completed, gateway('done')], false)!.statuses)
    .toEqual([{ status: 'completed', count: 1 }, { status: 'returned', count: 1 }]);
});

it('counts genuine failures on the head and keeps only reader actions exposed', () => {
  const failing = summarizeProgramBatch([failed, completed, handoff], false)!;
  expect(failing).toMatchObject({ attention: 'error', defaultCollapsed: true });
  expect(failing.statuses).toEqual([{ status: 'completed', count: 1 },
    { status: 'handoff', count: 1 }, { status: 'failed', count: 1 }]);
  expect(summarizeProgramBatch([failed, running], true)).toMatchObject({ attention: 'active', defaultCollapsed: true });
  // An execution child that awaits the reader keeps the batch open.
  const awaiting = child('pending_approval', { toolName: 'run_command', attentionKind: 'interactive' });
  expect(summarizeProgramBatch([completed, completed], false, [completed, completed, awaiting]))
    .toMatchObject({ attention: 'interactive', defaultCollapsed: false });
});
