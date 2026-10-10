/** Edit failures expose retained evidence without guessing that a patch ran. */
import { describe, expect, it, vi } from 'vitest';
import { createToolResultPresentation } from './tool-result-presentation';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');
const messages: Record<string, string> = {
  'tool.failed': 'failed',
  'tool.editFailureUnavailable': 'Error details were not retained for this edit.',
  'tool.resultDeferred': 'Full output loads when expanded',
};
const presentation = createToolResultPresentation({
  translate: (key, ..._args) => messages[key] || key,
  writeGateRefusal: { resolveRefusal: () => null, renderNoticeHtml: () => '' },
});
const header = { iconHtml: '', queryHtml: 'Edit example.py', rootPillHtml: '', badgeHtml: 'failed' };
const summaries = Array.from({ length: 6 }, () => ({
  path: 'example.py', operation: 'replace', status: 'fail', detail: '',
}));
const round = { roundNum: 1, toolName: 'edit_file', status: 'error',
  toolArgs: { edits: summaries.map(() => ({ path: 'example.py', operation: 'replace', anchor: 'old', content: 'new' })) } };

function inspect(html: string, check: (document: Document) => void): void {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`);
  try { check(dom.window.document); } finally { dom.window.close(); }
}

describe('batch edit failure evidence', () => {
  it.each(['text', 'sparse projection'] as const)('keeps a whole-call %s error with six empty summaries', format => {
    const reason = 'Error: write refused before edit outcomes\n<script>unsafe()</script>';
    const value = { ...round, toolContent: format === 'text' ? reason : JSON.stringify({ status: 'error', summary: reason }) };
    const metadata = { editSummaries: summaries };
    const before = JSON.stringify([value, metadata]);
    const html = presentation.renderWriteResultHtml(value, metadata, header);
    inspect(html, document => {
      const rows = document.querySelectorAll('.ptool-batch-fail');
      expect(rows).toHaveLength(6);
      for (const row of rows) {
        expect(row.querySelector('.ptool-batch-fail-detail')?.textContent).toBe(reason);
      }
      expect(document.querySelector('.ptool-batch-done-header')?.textContent).toContain('write refused');
      expect(document.querySelector('script, .bdiff-block')).toBeNull();
    });
    expect(JSON.stringify([value, metadata])).toBe(before);
  });

  it('prefers per-edit outcomes and keeps multiline recovery evidence', () => {
    const value = { ...round, toolContent: 'Applied 0/1 edits (1 failed)\n'
      + '[1] FAIL example.py [replace] [match-not-found]: Anchor missing\nMost similar block: <unsafe>' };
    const html = presentation.renderWriteResultHtml(value, { editSummaries: [{ ...summaries[0], detail: 'STALE DETAIL' }] }, header);
    inspect(html, document => {
      expect(document.querySelector('.ptool-edit-kind')?.textContent).toBe('match-not-found');
      expect(document.querySelector('.ptool-batch-fail-detail')?.textContent).toContain('Most similar block: <unsafe>');
      expect(document.body.textContent).not.toContain('STALE DETAIL');
      expect(document.querySelector('unsafe, .bdiff-block')).toBeNull();
    });
  });

  it('states missing evidence instead of emitting empty failed disclosures', () => {
    inspect(presentation.renderWriteResultHtml(round, { editSummaries: summaries }, header), document => {
      const bodies = document.querySelectorAll('.ptool-batch-fail-detail');
      expect(bodies).toHaveLength(6);
      for (const body of bodies) expect(body.textContent).toBe(messages['tool.editFailureUnavailable']);
    });
  });

  it.each(['error', 'done'] as const)('connects historical %s batches to the existing lazy result loader', status => {
    const value = { ...round, status, _snapshotToolResult: { preview: 'Error preview', chars: 4096 } };
    const html = presentation.renderWriteResultHtml(value, { editSummaries: summaries }, header);
    inspect(html, document => {
      const disclosure = document.querySelector('details[data-lazy-tool-result]');
      expect(disclosure?.querySelector('.ptool-result-body')).not.toBeNull();
      expect(disclosure?.querySelector('.ptool-batch-done-list')).not.toBeNull();
      expect(disclosure?.textContent).toContain(messages['tool.resultDeferred']);
      expect(document.querySelector('.bdiff-block')).toBeNull();
    });
  });

  it('bounds displayed and hover error text', () => {
    const html = presentation.renderWriteResultHtml({ ...round, toolContent: 'Error: ' + 'x'.repeat(12000) }, { editSummaries: summaries }, header);
    inspect(html, document => {
      expect(document.querySelector('.ptool-batch-fail-detail')!.textContent!.length).toBeLessThanOrEqual(2001);
      expect(document.querySelector('.ptool-batch-fail-reason')!.getAttribute('title')!.length).toBeLessThanOrEqual(4000);
    });
  });
});

it('unwraps the retained summary of a partial batch before displaying failed rows', () => {
  const value = { ...round, status: 'done', toolContent: JSON.stringify({ status: 'partial',
    summary: 'Applied 0/2 edits (2 failed)\n[1] FAIL example.py [replace]: Retained partial error\n'
      + 'Most similar block: <unsafe>\nRecovery: read current content\n'
      + '[2] FAIL example.py [replace]: Separate failure' }) };
  inspect(presentation.renderWriteResultHtml(value, { editSummaries: summaries.slice(0, 2) }, header), document => {
    const bodies = document.querySelectorAll('.ptool-batch-fail-detail');
    expect(bodies[0].textContent).toContain('Retained partial error');
    expect(bodies[0].textContent).toContain('Most similar block: <unsafe>\nRecovery: read current content');
    expect(bodies[0].textContent).not.toContain('Separate failure');
    expect(bodies[1].textContent).toContain('Separate failure');
    expect(document.querySelector('unsafe, .bdiff-block')).toBeNull();
  });
});

it('exposes a workspace-busy refusal without inventing edit execution', () => {
  const reason = 'Error [workspace-busy]: Workspace write is active: /workspaces/shared. '
    + 'No edits applied; retry after the writer finishes.';
  const value = { ...round, toolContent: JSON.stringify({ status: 'error', summary: reason }) };
  inspect(presentation.renderWriteResultHtml(value, { editSummaries: summaries }, header), document => {
    expect(document.querySelectorAll('.ptool-batch-fail-detail')).toHaveLength(6);
    expect(document.querySelector('.ptool-batch-fail-detail')?.textContent).toBe(reason);
    expect(document.querySelector('.ptool-batch-done-header')?.textContent).toContain('workspace-busy');
    expect(document.querySelector('.ptool-batch-ok, .bdiff-block')).toBeNull();
  });
});
