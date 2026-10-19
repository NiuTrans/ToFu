/** Executable revision-request journey using actual edit markup and scoped gestures. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createLifecycleScope } from '../../lifecycle';
import { createToolResultPresentation } from '../../conversation/presentation/tool-result-presentation';
import { mountToolDiffReview } from '../../conversation/ui/tool-diff-review';
import type { ConversationTurnViewModel, ToolBlockViewModel } from '../../conversation/presentation/view-model/types';
import { installDiffReview } from './diff-review';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Window & typeof globalThis };
}>('jsdom');
const disposals: (() => void)[] = [];
afterEach(() => { for (const dispose of disposals.splice(0)) dispose(); });
const text = (_key: string, fallback: string) => fallback;
const presentation = createToolResultPresentation({ translate: (key, ..._args) => key,
  writeGateRefusal: { resolveRefusal: () => null, renderNoticeHtml: () => '' } });
const header = { iconHtml: '', queryHtml: 'Edit file', rootPillHtml: '', badgeHtml: 'done' };

function fixture(args: Record<string, unknown> = { path: 'src/example.ts', search: 'old', replace: 'new' },
  meta: Record<string, unknown> = {}, name = 'apply_diff', status = 'done') {
  const dom = new JSDOM('<body><main id="tools"></main><textarea id="userInput"></textarea></body>');
  const document = dom.window.document, node = document.getElementById('tools')!;
  const input = document.getElementById('userInput') as HTMLTextAreaElement;
  const scope = createLifecycleScope(dom.window as unknown as Window);
  const round = { toolName: name, toolCallId: 'call-1', roundNum: 1, toolArgs: args, status, results: [meta] };
  const block = { kind: 'tool', name, toolCallId: 'call-1', input: args,
    result: { status }, round } as unknown as ToolBlockViewModel;
  const turn = { turnId: 'turn-1', projectionRevision: 4,
    source: { conversationId: 'conv-1' } } as ConversationTurnViewModel;
  const redraw = () => {
    node.innerHTML = presentation.renderWriteResultHtml(round, meta, header);
    mountToolDiffReview(node, block, turn, text);
  };
  redraw();
  let context: string | null = 'conv-1';
  const quotes: string[] = [], errors: unknown[] = [];
  installDiffReview(scope, document, input, { contextIdentity: () => context, text,
    addQuote: quote => quotes.push(quote), reportFailure: error => errors.push(error) });
  disposals.push(() => { scope.destroy(); dom.window.close(); });
  const button = () => node.querySelector<HTMLButtonElement>('[data-diff-review-action]')!;
  const pointer = (target = button()) => {
    const event = new dom.window.Event('pointerdown', { bubbles: true });
    Object.defineProperty(event, 'button', { value: 0 }); target.dispatchEvent(event);
  };
  const select = (first: Element, last: Element = first) => {
    const range = document.createRange(); range.setStart(first.firstChild!, 0);
    range.setEnd(last.firstChild!, last.textContent!.length);
    document.getSelection()!.removeAllRanges(); document.getSelection()!.addRange(range);
  };
  return { dom, document, node, input, scope, quotes, errors, block, turn, redraw, button, pointer, select,
    switchContext: () => { context = 'conv-2'; } };
}

describe('request a revision from rendered edits', () => {
  it('adds a frozen file-specific quote, preserving the draft and focusing ordinary typing', () => {
    const f = fixture(); f.input.value = 'Keep my existing request.\n';
    f.pointer(); f.document.getSelection()?.removeAllRanges(); f.button().click();
    expect(f.quotes).toHaveLength(1);
    expect(f.quotes[0]).toContain('src/example.ts');
    expect(f.quotes[0]).toContain('-old\n+new');
    expect(f.quotes[0]).toContain('Read and check the current file');
    expect(f.quotes[0]).toContain('"toolCall":"call-1"');
    expect(f.input.value).toBe('Keep my existing request.\n');
    expect(f.document.activeElement).toBe(f.input);
    f.redraw(); expect(f.quotes[0]).toContain('-old\n+new');
    expect(f.errors).toHaveLength(0);
  });
  it('captures selected complete lines before the pointer clears native selection', () => {
    const f = fixture({ path: 'example.ts', search: 'keep\nold\ntail', replace: 'keep\nnew\ntail' });
    f.select(f.node.querySelector('.bdiff-add code')!); f.pointer();
    f.document.getSelection()!.removeAllRanges(); f.button().click();
    expect(f.quotes[0]).toContain('+new');
    expect(f.quotes[0]).not.toContain('-old');
    expect(f.quotes[0]).not.toContain(' tail');
  });
  it('keeps batch attribution across failed rows and repeated paths in different roots', () => {
    const args = { edits: [
      { path: 'a.ts', root: 'first', search: 'a', replace: 'b' },
      { path: 'a.ts', root: 'second', search: 'c', replace: 'd' },
    ] };
    const f = fixture(args, { editSummaries: [
      { path: 'a.ts', status: 'fail', detail: 'missing' },
      { path: 'a.ts', status: 'ok' },
    ] }, 'apply_diffs');
    expect(f.node.querySelectorAll('[data-diff-review-action]')).toHaveLength(1);
    f.button().click(); expect(f.quotes[0]).toContain('second:a.ts');
    expect(f.quotes[0]).toContain('-c\n+d');
  });
  it('does not offer revision actions for refused writes or missing file identity', () => {
    expect(fixture(undefined, { writeOk: false }, 'apply_diff', 'error').button()).toBeNull();
    expect(fixture({ search: 'old', replace: 'new' }).button()).toBeNull();
  });
  it('rejects a cross-diff selection without adding a quote or changing the draft', () => {
    const f = fixture({ edits: [
      { path: 'a.ts', search: 'a', replace: 'b' }, { path: 'b.ts', search: 'c', replace: 'd' },
    ] }, { editSummaries: [{ path: 'a.ts', status: 'ok' }, { path: 'b.ts', status: 'ok' }] }, 'apply_diffs');
    const codes = f.node.querySelectorAll('.bdiff-block code');
    f.input.value = 'draft'; f.select(codes[0], codes[codes.length - 1]);
    f.pointer(); f.document.getSelection()!.removeAllRanges(); f.button().click();
    expect(f.quotes).toHaveLength(0); expect(f.input.value).toBe('draft');
    expect(String(f.errors[0])).toContain('within this change');
  });
  it('refuses oversized evidence intact, then accepts a smaller keyboard selection', () => {
    const f = fixture({ path: 'large.ts', content: Array.from({ length: 81 }, (_, i) => `row ${i}`).join('\n') }, {}, 'write_file');
    f.button().click(); expect(f.quotes).toHaveLength(0);
    expect(String(f.errors[0])).toContain('too large');
    f.select(f.node.querySelector('code')!); f.button().click();
    expect(f.quotes[0]).toContain('+row 0'); expect(f.quotes[0]).not.toContain('+row 1');
  });
  it('refuses excessive characters without silently clipping', () => {
    const f = fixture({ path: 'large.ts', content: 'x'.repeat(6001) }, {}, 'write_file');
    f.button().click(); expect(f.quotes).toHaveLength(0);
    expect(String(f.errors[0])).toContain('too large');
  });
  it('fences a conversation switch and a changed projection between gestures', () => {
    const f = fixture(); f.pointer(); f.switchContext(); f.button().click();
    expect(f.quotes).toHaveLength(0); expect(String(f.errors[0])).toContain('original conversation');
    const changed = fixture(); changed.pointer(); changed.turn.projectionRevision++;
    mountToolDiffReview(changed.node, changed.block, changed.turn, text); changed.button().click();
    expect(changed.quotes).toHaveLength(0); expect(String(changed.errors[0])).toContain('original conversation');
  });
  it('preserves equivalent redraws and releases gesture ownership on disposal', () => {
    const f = fixture(); f.pointer(); mountToolDiffReview(f.node, f.block, f.turn, text);
    f.button().click(); expect(f.quotes).toHaveLength(1);
    f.scope.destroy(); f.button().click(); expect(f.quotes).toHaveLength(1);
  });
  it('rejects unavailable input and ignores forged actions', () => {
    const f = fixture(); f.input.readOnly = true; f.button().click();
    expect(f.quotes).toHaveLength(0); expect(String(f.errors[0])).toContain('unavailable');
    const forged = f.document.createElement('button'); forged.dataset.diffReviewAction = 'quote';
    f.node.append(forged); forged.click(); expect(f.quotes).toHaveLength(0);
  });
  it('keeps embedded fences and HTML inert inside the quoted diff', () => {
    const f = fixture({ path: 'markup.ts', content: '```\n<script>example()</script>' }, {}, 'write_file');
    f.button().click(); expect(f.quotes[0]).toContain('````diff');
    expect(f.quotes[0]).toContain('+<script>example()</script>');
    expect(f.node.querySelector('script')).toBeNull();
  });
});
