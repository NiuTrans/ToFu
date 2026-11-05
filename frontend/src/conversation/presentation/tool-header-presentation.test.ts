import { describe, expect, it } from 'vitest';
import { createToolHeaderPresentation } from './tool-header-presentation';

const header = createToolHeaderPresentation({
  translate: (key, ..._args) => key,
  writeGateRefusal: {
    resolveRefusal: (_round, meta) => (meta as { refusal?: unknown }).refusal ? { kind: 'stale', paths: [], skipped: 1, proceeded: 0 } : null,
    renderBadgeHtml: () => 'refusal reason',
  },
});
const summaries = Array.from({ length: 6 }, (_, i) => ({ status: i === 3 ? 'fail' : 'ok', detail: 'reason' }));

describe('tool effect outcome badges', () => {
  it.each(['run_command', 'write_file', 'edit_file'])('keeps %s non-entry distinct from failure', toolName => {
    const value = header.renderBadgeHtml({ toolName, status: 'error', executionEffect: {
      operationId: 'a'.repeat(32), state: 'not_started', recovery: 'reconcile', scope: 'process',
    } }, { badge: 'failed' });
    expect(value).toContain('tool.notStarted');
    expect(value).not.toContain('tool.failed');
  });

  it('does not claim a definite failure when the effect outcome is unknown', () => {
    const value = header.renderBadgeHtml({ toolName: 'run_command', status: 'error', executionEffect: {
      operationId: 'a'.repeat(32), state: 'unknown', recovery: 'reconcile', scope: 'process',
    } }, {});
    expect(value).toContain('tool.effectUnknown');
    expect(value).toContain('ptool-badge-warn');
    expect(value).toContain('data-tool-effect-outcome="unknown"');
    expect(value).not.toContain('tool.failed');
  });
});

describe('batch header operation counts', () => {
  it.each(['edit_file', 'apply_diffs', 'insert_contents'])('counts outcomes for %s, not distinct files or tokens', toolName => {
    const round = Object.freeze({ toolName, status: 'done', toolTokens: 1000 });
    const meta = Object.freeze({ editSummaries: summaries, badge: '6/6 edits', writeOk: false });
    const before = JSON.stringify([round, meta]);
    const markup = header.renderBadgeHtml(round, meta);
    expect(markup).toContain('>5/6<');
    expect(markup).toContain('ptool-badge-err');
    expect(markup).not.toMatch(/reason|tok|edits/);
    expect(JSON.stringify([round, meta])).toBe(before);
  });

  it('makes complete success green', () => {
    const markup = header.renderBadgeHtml({ toolName: 'edit_file', status: 'done' }, {
      editSummaries: summaries.map(() => ({ status: 'ok' })), writeOk: true,
    });
    expect(markup).toContain('>6/6<');
    expect(markup).toContain('ptool-badge-ok');
  });

  it.each([{ writeOk: false }, { type: 'error' }, { refusal: { kind: 'stale' } }])('keeps conflicting failure verdicts red: %j', failure => {
    const markup = header.renderBadgeHtml({ toolName: 'edit_file', status: 'done' }, {
      ...failure, editSummaries: [{ status: 'ok' }],
    });
    expect(markup).toContain('>1/1<');
    expect(markup).toContain('ptool-badge-err');
    expect(markup).not.toContain('refusal reason');
  });

  it('does not treat unknown per-edit status as success', () => {
    expect(header.renderBadgeHtml({ toolName: 'edit_file' }, {
      editSummaries: [{ status: 'ok' }, {}, null],
    })).toMatch(/ptool-badge-err[^>]*>1\/3</);
  });

  it('recovers legacy counts without promoting invalid ratios', () => {
    expect(header.renderBadgeHtml({ toolName: 'edit_file' }, { badge: '2/3 edits' })).toContain('>2/3<');
    expect(header.renderBadgeHtml({ toolName: 'edit_file' }, { badge: '4/3 edits' })).toBe('');
  });

  it('reports zero applied on a known rejected request, not fake success from arguments', () => {
    const round = { toolName: 'edit_file', toolArgs: { edits: [{ path: 'a' }, { path: 'b' }] } };
    expect(header.renderBadgeHtml(round, {})).toBe('');
    expect(header.renderBadgeHtml({ ...round, status: 'rejected' }, {})).toContain('>0/2<');
    expect(header.renderBadgeHtml({ toolName: 'edit_file', status: 'error' }, {})).toContain('tool.failed');
  });
});
