/** One refusal → fallback → switch-back incident renders once, not five times. */
import { describe, expect, it } from 'vitest';
import type { TurnActivityEntry, TurnActivityTimeline } from '../../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from './types';
import { visibleActivityEntries } from './activity-blocks';

function entry(changes: Partial<TurnActivityEntry> & { id: string }): TurnActivityEntry {
  return {
    spanId: changes.id, seq: 1, occurredAt: 1000, kind: 'model', status: 'failed',
    severity: 'error', count: 1, ...changes,
  };
}

function timeline(entries: ReadonlyArray<TurnActivityEntry>): TurnActivityTimeline {
  return { blockId: 'activity-timeline', version: 1, entries };
}

function visible(entries: ReadonlyArray<TurnActivityEntry>) {
  return visibleActivityEntries(timeline(entries), [], []);
}

const REFUSAL = 'kunlunbedrock.claude-opus-5.5-m17 declined the request '
  + '(category: user_sensitive_words): user sensitive words detected: test_sensitive';

/** The exact sequence a single provider refusal produces in the timeline. */
function refusalIncident(): TurnActivityEntry[] {
  return [
    entry({
      id: 'attempt', spanId: 'model:ephemeral:1', parentSpanId: 'model:attempt-a:1',
      requestLevel: 'provider_attempt', attemptScope: 'dispatcher',
      model: 'kunlunbedrock.claude-opus-5.5-m17', providerId: 'example-corp',
      keyHint: '2031********4861', reasonCode: 'ModelRefusalError', durationMs: 3200,
    }),
    entry({
      id: 'logical', spanId: 'model:attempt-a:1', model: 'claude-opus-5.5',
      providerId: 'example-corp', keyHint: '2031********4861',
      summaryKey: 'activity.model.requestFailed',
      summaryArgs: { model: 'claude-opus-5.5' },
      reasonCode: 'ModelRefusalError', detail: REFUSAL, durationMs: 6700,
    }),
    entry({
      id: 'announce', kind: 'status', status: 'waiting', severity: 'warning',
      phase: 'retrying', summaryKey: 'stream.phase.refusalFallback',
      summaryArgs: { model: 'claude-opus-5.5', fallback: 'kimi-k3.1-pro' },
      reasonCode: 'retrying', model: 'kimi-k3.1-pro',
    }),
    entry({
      id: 'switch', status: 'switched', severity: 'warning',
      summaryKey: 'activity.model.switched',
      summaryArgs: { from: 'claude-opus-5.5', to: 'kimi-k3.1-pro' },
      fromModel: 'claude-opus-5.5', toModel: 'kimi-k3.1-pro',
      detail: `content_filter: ${REFUSAL}`, reasonCode: 'content_filter',
      action: 'fallback',
    }),
    entry({
      id: 'switchback', kind: 'status', status: 'waiting', severity: 'warning',
      phase: 'retrying', summaryKey: 'stream.phase.refusalSwitchback',
      summaryArgs: { model: 'claude-opus-5.5', fallback: 'kimi-k3.1-pro' },
      reasonCode: 'retrying', model: 'kimi-k3.1-pro',
    }),
  ];
}

describe('recovered failure chain presentation', () => {
  it('renders a whole refusal incident as one recovered row plus the probe note', () => {
    const rows = visible(refusalIncident());
    expect(rows.map(row => row.id)).toEqual(['switch', 'switchback']);
    const recovered = rows[0];
    // One cause, once, without the duplicated taxonomy prefix.
    expect(recovered.detail).toBe(REFUSAL);
    expect(recovered.status).toBe('switched');
    expect(recovered.severity).toBe('warning');
    expect(recovered.summaryKey).toBe('activity.model.switched');
    // Routing facts survive the fold; the logical request's duration wins
    // because the nested attempt's span is contained inside it.
    expect(recovered.providerId).toBe('example-corp');
    expect(recovered.keyHint).toBe('2031********4861');
    // The wire deployment stays visible even though the summary names the
    // selected model.
    expect(recovered.model).toBe('kunlunbedrock.claude-opus-5.5-m17');
    expect(recovered.durationMs).toBe(6700);
    expect(recovered.reasonCode).toBe('content_filter');
    expect(recovered.fromModel).toBe('claude-opus-5.5');
    expect(recovered.toModel).toBe('kimi-k3.1-pro');
  });

  it('never mutates the durable timeline', () => {
    const entries = refusalIncident();
    const snapshot = JSON.stringify(entries);
    visible(entries);
    expect(JSON.stringify(entries)).toBe(snapshot);
  });

  it('keeps an unrecovered failure as its own failed row', () => {
    const rows = visible(refusalIncident().slice(0, 2));
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe('logical');
    expect(rows[0].status).toBe('failed');
    expect(rows[0].model).toBe('kunlunbedrock.claude-opus-5.5-m17');
    expect(rows[0].providerId).toBe('example-corp');
    expect(rows[0].keyHint).toBe('2031********4861');
    expect(rows[0].detail).toBe(REFUSAL);
  });

  it('keeps a switch announcement when no typed switch record follows', () => {
    const rows = visible(refusalIncident().slice(0, 3));
    expect(rows.map(row => row.id)).toEqual(['logical', 'announce']);
  });

  it('keeps distinct retry attempts instead of collapsing a real retry storm', () => {
    const first = entry({
      id: 'attempt-1', spanId: 'model:ephemeral:1', parentSpanId: 'model:attempt-a:1',
      keyHint: 'key-one', reasonCode: 'RateLimitError', detail: 'HTTP 429',
      statusCode: 429,
    });
    const second = entry({
      id: 'attempt-2', spanId: 'model:ephemeral:2', parentSpanId: 'model:attempt-a:1',
      keyHint: 'key-two', reasonCode: 'RateLimitError', detail: 'HTTP 429',
      statusCode: 429,
    });
    const logical = entry({
      id: 'logical', spanId: 'model:attempt-a:1', model: 'claude-opus-5.5',
      reasonCode: 'RateLimitError', detail: 'HTTP 429', statusCode: 429,
    });
    const rows = visible([first, second, logical]);
    expect(rows.map(row => row.id)).toEqual(['attempt-1', 'logical']);
    expect(rows[0].keyHint).toBe('key-one');
    expect(rows[1].keyHint).toBe('key-two');
  });

  it('keeps a failure that is not adjacent to its switch', () => {
    const [attempt, logical, announce, , switchback] = refusalIncident();
    const rows = visible([attempt, logical, switchback, announce]);
    expect(rows.map(row => row.id)).toEqual(['logical', 'switchback', 'announce']);
  });

  it('does not absorb a failure recorded against a different model', () => {
    const [, logical, announce, switched] = refusalIncident();
    const rows = visible([
      {
        ...logical, model: 'another-model',
        summaryArgs: { model: 'another-model' },
      },
      announce, switched,
    ]);
    expect(rows.map(row => row.id)).toEqual(['logical', 'switch']);
  });

  it('leaves unrelated tool and status rows untouched', () => {
    const tool = entry({
      id: 'tool', kind: 'tool', status: 'failed', severity: 'error',
      toolName: 'run_command', summaryKey: 'activity.tool.failed',
    });
    const rows = visible([tool, ...refusalIncident()]);
    expect(rows.map(row => row.id)).toEqual(['tool', 'switch', 'switchback']);
    expect(rows[0]).toBe(tool);
  });
});

describe('a tool whose completion frame was lost', () => {
  const span = entry({
    id: 'tool-span', kind: 'tool', status: 'aborted', severity: 'warning',
    toolName: 'run_command', toolCallId: 'call-1', summaryKey: 'activity.tool.started',
    summaryArgs: { tool: 'run_command' },
  });

  function rendered(roundStatus: string): ConversationBlockViewModel[] {
    return [{
      kind: 'tool', blockId: 'tool-block', identitySource: 'contract',
      source: { blockId: 'tool-block' }, toolCallId: 'call-1', name: 'run_command',
      input: {}, result: {},
      round: { toolCallId: 'call-1', toolName: 'run_command', status: roundStatus },
    } as unknown as ConversationBlockViewModel];
  }

  it.each(['done', 'aborted', 'error'])('drops the row the %s card already owns', status => {
    expect(visibleActivityEntries(timeline([span]), [], rendered(status))).toEqual([]);
  });

  it('keeps the row while the owning card is still running', () => {
    expect(visibleActivityEntries(timeline([span]), [], rendered('running'))).toEqual([span]);
  });

  it('keeps the row when no rendered occurrence can own it', () => {
    expect(visibleActivityEntries(timeline([span]), [], rendered('done')
      .map(block => ({ ...block, toolCallId: 'another-call' })))).toEqual([span]);
  });
});
