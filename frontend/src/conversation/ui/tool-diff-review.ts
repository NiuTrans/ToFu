/** Quote rendered edit evidence into the ordinary composer; never edits files or sends.
 * Entry points: mountToolDiffReview, captureToolDiffReview, formatToolDiffReview.
 * Dependencies: projected tool/turn inputs and the shared button factory.
 * Weak button bindings expire with their rendered diff, with no observer or cache.
 */
import { createButton } from '../../ui';
import type { ConversationTurnViewModel, ToolBlockViewModel } from '../presentation/view-model/types';

export type DiffReviewText = (key: string, fallback: string) => string;
interface DiffReviewContext {
  conversationId: string;
  turnId: string;
  toolCallId: string;
  revision: number;
  path: string;
  root: string;
}
interface Binding { diff: HTMLElement; context: DiffReviewContext }
export interface DiffReviewQuote {
  readonly context: Readonly<DiffReviewContext>;
  readonly code: string;
  readonly selected: boolean;
  readonly binding: object;
}
const bindings = new WeakMap<HTMLButtonElement, Binding>();
const MAX_QUOTE_CHARS = 6000;
const MAX_QUOTE_LINES = 80;
const WRITE_TOOLS = new Set(['write_file', 'apply_diff', 'insert_content', 'edit_file', 'apply_diffs', 'insert_contents']);

function record(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try { return record(JSON.parse(value)); } catch { return {}; }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
function string(value: unknown): string { return typeof value === 'string' ? value : ''; }

/** Only actual write-result diffs receive an action. Failed rows and paper proposals do not. */
export function mountToolDiffReview(
  node: HTMLElement, block: ToolBlockViewModel, turn: ConversationTurnViewModel,
  text: DiffReviewText,
): void {
  if (!WRITE_TOOLS.has(block.name) || !turn.source.conversationId) return;
  const args = record(block.input ?? block.round?.toolArgs);
  const meta = record(block.round?.results?.[0]);
  const failed = ['error', 'rejected', 'aborted', 'unanswerable'].includes(
    String(block.round?.status ?? block.result?.status ?? '')) || meta.writeOk === false;
  const summaries = Array.isArray(meta.editSummaries) ? meta.editSummaries.map(record) : [];
  const edits = Array.isArray(args.edits) ? args.edits.map(record) : [];
  const rows = Array.from(node.querySelectorAll<HTMLElement>('.ptool-batch-done-edit'));
  for (const diff of node.querySelectorAll<HTMLElement>('.bdiff-block')) {
    const row = diff.closest<HTMLElement>('.ptool-batch-done-edit');
    if (row ? !row.classList.contains('ptool-batch-ok') : failed) continue;
    const index = row ? rows.indexOf(row) : -1;
    const edit = index >= 0 ? edits[index] ?? {} : args;
    const summary = index >= 0 ? summaries[index] ?? {} : {};
    const path = string(summary.path) || string(edit.path) || string(args.path);
    if (!path || path.length > 1024) continue;
    const context: DiffReviewContext = {
      conversationId: turn.source.conversationId, turnId: turn.turnId,
      toolCallId: block.toolCallId, revision: turn.projectionRevision, path,
      root: (string(summary.root) || string(edit.root) || string(args.root)).slice(0, 256),
    };
    let toolbar = diff.nextElementSibling as HTMLElement | null;
    if (!toolbar?.classList.contains('tool-diff-review')) {
      toolbar = node.ownerDocument.createElement('div');
      toolbar.className = 'tool-diff-review';
      const button = createButton({ label: text('diffReview.action', 'Request a change'),
        variant: 'ghost', size: 'sm', wrap: true }, node.ownerDocument);
      button.dataset.diffReviewAction = 'quote';
      toolbar.append(button);
      diff.after(toolbar);
    }
    const button = toolbar.querySelector<HTMLButtonElement>('[data-diff-review-action]')!;
    button.textContent = text('diffReview.action', 'Request a change');
    button.title = text('diffReview.hint', 'Select code to quote specific lines, or quote this change. Then add your request in the message box.');
    const prior = bindings.get(button);
    // Preserve a pointer/keyboard gesture across unrelated equivalent redraws.
    if (!prior || prior.diff !== diff || JSON.stringify(prior.context) !== JSON.stringify(context)) {
      bindings.set(button, { diff, context });
    }
  }
}

export function captureToolDiffReview(button: HTMLButtonElement, text: DiffReviewText): DiffReviewQuote | null {
  const binding = bindings.get(button);
  if (!binding || !button.isConnected || !binding.diff.isConnected) return null;
  const selection = button.ownerDocument.getSelection();
  const selected = Boolean(selection && !selection.isCollapsed && selection.toString());
  const range = selected && selection?.rangeCount === 1 ? selection.getRangeAt(0) : null;
  if (selected && (!range || !binding.diff.contains(range.startContainer)
    || !binding.diff.contains(range.endContainer))) {
    throw new Error(text('diffReview.oneChange', 'Select code within this change only.'));
  }
  const lines: string[] = [];
  let chars = 0;
  for (const row of binding.diff.querySelectorAll<HTMLElement>(':scope > .bdiff-line')) {
    const code = row.querySelector('code');
    if (!code || (range && !range.intersectsNode(code))) continue;
    const line = `${row.classList.contains('bdiff-del') ? '-' : row.classList.contains('bdiff-add') ? '+' : ' '}${code.textContent ?? ''}`;
    chars += line.length + 1;
    if (chars > MAX_QUOTE_CHARS || lines.length >= MAX_QUOTE_LINES) {
      throw new Error(text('diffReview.tooLarge', 'This quote is too large. Select a smaller set of code lines.'));
    }
    lines.push(line);
  }
  if (!lines.length) return null;
  return { context: { ...binding.context }, code: lines.join('\n'), selected, binding };
}

export function isToolDiffReviewCurrent(button: HTMLButtonElement, quote: DiffReviewQuote): boolean {
  return button.isConnected && bindings.get(button) === quote.binding;
}

export function formatToolDiffReview(quote: DiffReviewQuote, text: DiffReviewText): string {
  const runs = quote.code.match(/`+/g) ?? [];
  const fence = '`'.repeat(Math.max(3, ...runs.map(run => run.length + 1)));
  const context = quote.context;
  const source = JSON.stringify({
    file: context.path, ...(context.root ? { root: context.root } : {}), conversation: context.conversationId,
    turn: context.turnId, toolCall: context.toolCallId, revision: context.revision,
  });
  return `${text('diffReview.file', 'File:')} ${context.root ? `${context.root}:` : ''}${context.path}\n${fence}diff\n${quote.code}\n${fence}\n${text('diffReview.currentFile', 'This is the code shown in the earlier change. Read and check the current file before editing; this excerpt has no absolute file line numbers.')}\n${text('diffReview.reference', 'Change reference')}: ${source}`;
}
