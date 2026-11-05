import { describe, expect, it, vi } from 'vitest';
import {
  createReportEventProjection, makeReportStreamState, reportSegmentsForRender,
  type ReportEventPorts, type ReportEventView,
} from './report-events';
import { paperIngestEvent, taskReplayIngestPage } from './push-transport';

function fixture(kind = 'report', active = true, overrides: Partial<ReportEventPorts> = {}) {
  const state = makeReportStreamState('paper-a', 'en', 'task-a', kind);
  const view: ReportEventView = { cache: '', meta: null, langKey: () => `${kind}:en` };
  const ports: ReportEventPorts = {
    isActivePaper: id => active && id === 'paper-a',
    viewFor: vi.fn(() => view),
    setPaperHash: vi.fn(), rememberSnapshot: vi.fn(),
    persistReviewVenue: vi.fn(), applyResolvedTitle: vi.fn(),
    ...overrides,
  };
  return { state, view, ports, apply: createReportEventProjection(ports) };
}

describe('report event projection', () => {
  it('reconciles canonical report snapshots without losing or duplicating addenda', () => {
    const { state, view, apply } = fixture();
    apply(state, { type: 'done', report: 'body' });
    apply(state, { type: 'insight', insight: 'insight' });
    apply(state, { type: 'termfill', addendum: 'glossary' });
    apply(state, { type: 'done', report: 'canonical body' });
    apply(state, { type: 'insight', insight: 'insight' });
    apply(state, { type: 'termfill', addendum: 'glossary' });
    expect(state.fullText).toBe('canonical body\n\ninsight\n\nglossary\n');
    expect(view.cache).toBe(state.fullText);
  });

  it('keeps reasoning and tool narration while removing only the reset round draft', () => {
    const { state, apply } = fixture();
    apply(state, { type: 'thinking', delta: 'reason ', llmRound: 0 });
    apply(state, { type: 'thinking', delta: 'more', llmRound: 0 });
    apply(state, { type: 'delta', delta: 'draft', llmRound: 0 });
    apply(state, { type: 'tool_start', roundNum: 1, toolCallId: 'search', llmRound: 0 });
    apply(state, { type: 'delta', delta: 'next narration', llmRound: 1 });
    apply(state, { type: 'tool_start', roundNum: 2, toolCallId: 'read', llmRound: 1 });
    apply(state, { type: 'delta_reset', llmRound: 0 });
    expect(state.fullText).toBe('');
    expect(state.contentStarted).toBe(false);
    expect(state.segments).toEqual([
      { type: 'thinking', text: 'reason more', llmRound: 0 },
      { type: 'tool_use', id: 'search', llmRound: 0 },
      { type: 'text', text: 'next narration', llmRound: 1 },
      { type: 'tool_use', id: 'read', llmRound: 1 },
    ]);
    apply(state, { type: 'thinking', delta: 'final reasoning', llmRound: 2 });
    apply(state, { type: 'delta', delta: 'final report', llmRound: 2 });
    expect(reportSegmentsForRender(state).at(-1)).toEqual({
      type: 'thinking', text: 'final reasoning', llmRound: 2,
    });
    expect(state.fullText).toBe('final report');
  });

  it('infers older-server dispatch rounds and ignores empty prose', () => {
    const { state, apply } = fixture();
    apply(state, { type: 'thinking', delta: '' });
    apply(state, { type: 'thinking', delta: 'before' });
    apply(state, { type: 'tool_start', toolCallId: 'first' });
    apply(state, { type: 'delta', delta: 'after' });
    expect(state.segments.map(segment => segment.llmRound)).toEqual([0, 0, 1]);
    expect(reportSegmentsForRender(state)).toHaveLength(2);
  });

  it('projects mixed tools and their raw debugging evidence into chat-shaped rounds', () => {
    const { state, apply } = fixture();
    ['web_search', 'read_files', 'run_command', 'todo_write', 'new_tool'].forEach((name, i) => {
      apply(state, { type: 'tool_start', roundNum: i, llmRound: 0, toolName: name,
        toolCallId: `call-${i}`, toolArgs: { query: i }, attentionKind: 'important',
        parentToolCallId: 'parent' });
    });
    const evidence = [{ title: 'result', snippet: 'debug evidence' }];
    apply(state, { type: 'tool_done', roundNum: 2, elapsed: 1.26,
      toolContent: { raw: 'tool bytes' }, results: evidence,
      searchDiag: { retries: 2 }, engineBreakdown: { engine: 1 },
      vertical: 'web', verticals: ['web'], cacheSource: 'shared' });
    expect(state.toolRounds[2]).toMatchObject({
      toolName: 'run_command', toolCallId: 'call-2', toolArgs: { query: 2 },
      attentionKind: 'important', parentToolCallId: 'parent', status: 'done',
      toolContent: { raw: 'tool bytes' }, results: evidence, _elapsed: '1.3s',
      searchDiag: { retries: 2 }, engineBreakdown: { engine: 1 },
      vertical: 'web', verticals: ['web'], cacheSource: 'shared',
    });
    expect(state.toolRounds.filter(round => round.status === 'searching')).toHaveLength(4);
    apply(state, { type: 'tool_done', roundNum: 99 });
    apply(state, { type: 'tool_progress', roundNum: 99, chunk: 'orphan' });
    expect(state.toolRounds).toHaveLength(5);
  });

  it('deduplicates tool progress and retains a bounded tail with explicit loss evidence', () => {
    const { state, apply } = fixture();
    apply(state, { type: 'tool_start', roundNum: 1 });
    apply(state, { type: 'tool_progress', roundNum: 1, seq: 1, chunk: 'a'.repeat(100000) });
    apply(state, { type: 'tool_progress', roundNum: 1, seq: 1, chunk: 'duplicate' });
    expect(state.toolRounds[0]._partialOutput).toHaveLength(100000);
    apply(state, { type: 'tool_progress', roundNum: 1, seq: 2, chunk: 'z',
      contractVersion: 'tofu.tool-progress/v1', stream: 'stdout', spooling: true,
      grepSearchIntercepted: true });
    expect(state.toolRounds[0]).toMatchObject({
      _partialOutputTotalChars: 100001, _partialOutputTruncated: true,
      _partialOutputStatus: 'overflow-loss', _partialOutputSeq: 2,
      _partialOutputSpooling: true, _partialOutputContractVersion: 'tofu.tool-progress/v1',
      _partialOutputStream: 'stdout', grepSearchIntercepted: true,
    });
    expect(state.toolRounds[0]._partialOutput).toBe(
      '\n\n… [live output truncated] …\n\n' + 'a'.repeat(24999) + 'z');
    apply(state, { type: 'tool_progress', roundNum: 1, seq: 3,
      chunk: 'tail', terminalReason: 'cancelled' });
    expect(state.toolRounds[0]._partialOutputStatus).toBe('cancelled-partial');
    expect(state.toolRounds[0]._partialOutput?.match(/live output truncated/g)).toHaveLength(1);
    expect(state.toolRounds[0]._partialOutput?.endsWith('ztail')).toBe(true);
    apply(state, { type: 'tool_done', roundNum: 1, toolContent: 'authoritative result' });
    expect(state.toolRounds[0].toolContent).toBe('authoritative result');
  });

  it('retains no-sequence progress and cancellation status through a backend truncation marker', () => {
    const { state, apply } = fixture();
    apply(state, { type: 'tool_start', roundNum: 0 });
    apply(state, { type: 'tool_progress', roundNum: 0, chunk: 'a', spooling: true });
    expect(state.toolRounds[0]._partialOutputStatus).toBe('spooling');
    apply(state, { type: 'tool_progress', roundNum: 0, chunk: '\n\n… [live output truncated by server] …\n\ntail' });
    apply(state, { type: 'tool_progress', roundNum: 0, chunk: 'b',
      truncated: true, terminalReason: 'cancelling' });
    expect(state.toolRounds[0]._partialOutput).toBe('\n\n… [live output truncated] …\n\ntailb');
    expect(state.toolRounds[0]._partialOutputStatus).toBe('cancelling');
  });

  it.each(['report', 'review', 'rebuttal'])('publishes a completed %s through its own view ports', kind => {
    const { state, view, ports, apply } = fixture(kind);
    const meta = { model: 'model-a' };
    apply(state, { type: 'done', report: '# Done', meta, paperHash: 'hash', resolvedTitle: 'Title' });
    expect(state.status).toBe('done');
    expect(state.fullText).toBe('# Done');
    expect(view.cache).toBe('# Done');
    expect(view.meta).toBe(meta);
    expect(ports.rememberSnapshot).toHaveBeenCalledWith(view, '# Done', meta);
    expect(ports.persistReviewVenue).toHaveBeenCalledWith(view, `${kind}:en`, 'paper-a');
    expect(ports.setPaperHash).toHaveBeenCalledWith('hash');
    expect(ports.applyResolvedTitle).toHaveBeenCalledWith('Title', 'paper-a');
  });

  it('keeps background-paper events from publishing active cache, metadata or hash', () => {
    const { state, view, ports, apply } = fixture('report', false);
    apply(state, { type: 'enriched', text: 'enriched', paperHash: 'old-hash' });
    apply(state, { type: 'done', report: 'old report', meta: { model: 'old' }, paperHash: 'old-hash' });
    apply(state, { type: 'report_meta', meta: { cost: 2 } });
    apply(state, { type: 'insight', insight: 'insight' });
    apply(state, { type: 'termfill', addendum: 'glossary' });
    expect(state.fullText).toContain('glossary');
    expect(view.cache).toBe('');
    expect(view.meta).toBeNull();
    expect(ports.setPaperHash).not.toHaveBeenCalled();
    expect(ports.rememberSnapshot).not.toHaveBeenCalled();
    expect(ports.persistReviewVenue).not.toHaveBeenCalled();
  });

  it('applies legacy postpasses once while clearing fulfilled glossary warnings', () => {
    const { state, view, ports, apply } = fixture();
    apply(state, { type: 'done', report: 'body  ', meta: { terminologyAudit: { missing: 1 } } });
    apply(state, { type: 'insight_start' });
    expect(state._insightRunning).toBe(true);
    apply(state, { type: 'insight', insight: 'insight' });
    apply(state, { type: 'insight', insight: 'duplicate' });
    expect(state._insightRunning).toBe(false);
    apply(state, { type: 'termfill', addendum: 'glossary' });
    apply(state, { type: 'termfill', addendum: 'duplicate' });
    expect(state.fullText).toBe('body\n\ninsight\n\nglossary\n');
    expect(view.cache).toBe(state.fullText);
    expect(state.meta?.terminologyAudit).toBeNull();
    expect(view.meta?.terminologyAudit).toBeNull();
    expect(ports.rememberSnapshot).toHaveBeenCalledTimes(3);
  });

  it('delegates structured postpasses to the reading experience without a second body append', () => {
    const handler = vi.fn(() => true);
    const { state, view, apply } = fixture('review', true, {
      handleInsight: handler, handleCheckpoints: handler,
    });
    for (const type of ['insight', 'checkpoints']) {
      const event = { type, insight: 'structured', items: [{ anchor_idx: 2 }], meta: { cost: 1 } };
      expect(apply(state, event)).toBe(true);
      expect(handler).toHaveBeenLastCalledWith(state, event);
    }
    expect(state.fullText).toBe('');
    expect(state.meta).toBeNull();
    expect(view.cache).toBe('');
  });

  it.each(['report', 'review', 'rebuttal'])('owns late %s metadata without calling reading extensions', kind => {
    for (const active of [true, false]) {
      const handler = vi.fn(() => true);
      const { state, view, apply } = fixture(kind, active, {
        handleInsight: handler, handleCheckpoints: handler,
      });
      const meta = { model: 'late', totalCostCny: 0.015 };
      apply(state, { type: 'report_meta', meta });
      expect(state.meta).toBe(meta);
      expect(view.meta).toBe(active ? meta : null);
      expect(handler).not.toHaveBeenCalled();
    }
  });

  it('preserves partial cancellation without caching or publication', () => {
    const { state, view, ports, apply } = fixture();
    apply(state, { type: 'delta', delta: 'draft' });
    apply(state, { type: 'aborted', partial: 'server partial' });
    expect(state.status).toBe('aborted');
    expect(state.fullText).toBe('server partial');
    expect(state.contentStarted).toBe(true);
    expect(view.cache).toBe('');
    expect(ports.rememberSnapshot).not.toHaveBeenCalled();
    apply(state, { type: 'aborted', partial: '' });
    expect(state.fullText).toBe('server partial');
  });

  it('retains the full error envelope alongside its readable message', () => {
    const envelope = { message: 'Provider unavailable', code: 'provider_failed' };
    const { state, apply } = fixture('report', true, {
      normalizeError: () => envelope, errorMessage: () => envelope.message,
    });
    apply(state, { type: 'error', error: envelope });
    expect(state.status).toBe('error');
    expect(state.error).toBe(envelope.message);
    expect(state._errorEnv).toBe(envelope);
  });

  it.each([['failure', 'failure'], [{ code: 'opaque' }, 'Unknown error']])(
    'supports legacy errors without optional display ports', (error, message) => {
      const { state, apply } = fixture();
      apply(state, { type: 'error', error });
      expect(state.error).toBe(message);
      expect(state._errorEnv).toBeNull();
    },
  );

  it('shares exactly-once push/poll ingestion and accepts a server cursor reset', () => {
    const { state, apply } = fixture();
    const event = { type: 'delta', seq: 5, delta: 'once' };
    paperIngestEvent(state, event, apply);
    taskReplayIngestPage(state, { events: [event], next_cursor: 6 }, apply);
    expect(state.fullText).toBe('once');
    taskReplayIngestPage(state, { events: [{ type: 'delta_reset', seq: 1, llmRound: 0 },
      { type: 'delta', seq: 2, delta: 'recovered' }], cursor: { reset: true, next: 3 } }, apply);
    expect(state.fullText).toBe('recovered');
    expect(state._seqSeen).toBe(2);
  });

  it('leaves unknown events untouched and preserves skipped-pass semantics', () => {
    const { state, apply } = fixture();
    const before = structuredClone(state);
    expect(apply(state, { type: 'future_event' })).toBe(false);
    expect(state).toEqual(before);
    apply(state, { type: 'insight_start' });
    apply(state, { type: 'insight_skipped' });
    expect(state._insightRunning).toBe(false);
    for (const type of ['termfill_skipped', 'checkpoints', 'checkpoints_skipped']) {
      expect(apply(state, { type })).toBe(true);
    }
    expect(state.fullText).toBe('');
  });
});
