import { describe, expect, it } from 'vitest';
import { programExecutionPresentation } from './program-execution-presentation';

describe('program execution evidence', () => {
  it('separates successful reasoning handoff from running or failed execution', () => {
    const row = { programResult: { kind: 'observation', reason: 'choose a candidate' },
      programStats: { outcome: 'needs_model' } };
    expect(programExecutionPresentation({ ...row, programStatus: 'completed' }).needsModel).toBe(true);
    for (const programStatus of ['running', 'error', 'incomplete']) {
      expect(programExecutionPresentation({ ...row, programStatus }).needsModel).toBe(false);
    }
  });

  it('keeps business observation-shaped data separate from a runtime handoff', () => {
    const programResult = { kind: 'observation', reason: 'business record', data: 7 };
    expect(programExecutionPresentation({ programStatus: 'completed', programResult,
      programStats: { outcome: 'completed' } }).needsModel).toBe(false);
    expect(programExecutionPresentation({ programStatus: 'completed', programResult }).needsModel).toBe(false);
    expect(programExecutionPresentation({ programStats: { result_reads: 3 } }).resultReads).toBe(3);
    expect(programExecutionPresentation({ programStats: { result_reads: '3' } }).resultReads).toBeNull();
  });

  it('shows measured byte counts including growth and zero, without savings estimates', () => {
    expect(programExecutionPresentation({ programStats: {
      result_delivery: { outputBytes: 0, truncatedResults: 0 }, result_bytes: 250,
      call_stages: 0, parallel_stages: 0,
    } })).toMatchObject({ privateBytes: 0, resultBytes: 250, callStages: 0 });
  });

  it('leaves unmeasured native and legacy executions unmeasured', () => {
    expect(programExecutionPresentation({ programStatus: 'completed' }))
      .toMatchObject({ needsModel: false, privateBytes: null, resultBytes: null,
        callStages: null, parallelStages: null, truncatedResults: null });
  });

  it('rejects malformed and nonfinite telemetry', () => {
    expect(programExecutionPresentation({ programStats: {
      result_delivery: { outputBytes: -1, truncatedResults: '4' },
      result_bytes: Infinity, call_stages: NaN, parallel_stages: 1.2,
    } })).toMatchObject({ privateBytes: null, resultBytes: null,
      callStages: null, parallelStages: null, truncatedResults: null });
  });
});


describe('observed successful calls', () => {
  it('keeps zero success distinct from absent historical evidence', () => {
    expect(programExecutionPresentation({ programStats: { tool_calls: 1, successful_tool_calls: 0 } }))
      .toMatchObject({ toolCallAttempts: 1, successfulToolCalls: 0 });
    expect(programExecutionPresentation({ programStats: { tool_calls: 1 } }).successfulToolCalls).toBeNull();
  });
  it.each([null, -1, 2, '1', NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])('rejects contradictory or malformed success %s', value => {
    expect(programExecutionPresentation({ programStats: { tool_calls: 1, successful_tool_calls: value } }).successfulToolCalls).toBeNull();
  });
  it('only identifies name failure inside an actual runtime handoff', () => {
    const row = { programStatus: 'completed', programStats: { outcome: 'needs_model' },
      programResult: { kind: 'observation', reason: 'tool_not_done', data: { error: { code: 'tool_not_enabled' } } } };
    expect(programExecutionPresentation(row).toolFailureCode).toBe('tool_not_enabled');
    expect(programExecutionPresentation({ ...row, programStats: { outcome: 'completed' } }).toolFailureCode).toBe('');
  });
});
