/** Paper source tree + code view for the chat preview drawer.
 *
 * The tree comes from the build's frozen sources.json manifest (what was
 * actually compiled); file contents come from the owned document source API.
 * Track B: text files open in a lazy CodeMirror 6 editor with a fenced save
 * flow; binary files, truncated reads and every editor-load failure keep the
 * original read-only highlighted view.
 */
import type { PaperBuildApi } from '../../api/paper-build';
import { paperBuildArtifactPath } from '../../api/paper-build';
import type { PaperDocumentsApi } from '../../api/paper-documents';
import { apiFailure } from '../../api/errors';
import type { Translator } from '../../i18n';
import { createPaperSourceEditor, type PaperSourceEditor, type SourceEditorLanguage } from './source-editor';

export interface SourceViewContext {
  api: PaperBuildApi;
  documentsApi: PaperDocumentsApi;
  documentId: string;
  buildId: string;
  tr: Translator;
  /** Re-open the tree listing (the drawer's back affordance). */
  showTree(): void;
  /** Open one source file at an optional 1-based line. */
  showFile(path: string, line?: number): void;
  /** Successful source save hook (the panel nudges a rebuild here). */
  onSourceSaved?(): void;
  /** Bubble-menu chat handoff: prefill + send the main composer. */
  submitChat?(message: string): void;
  /** Dirty-guard confirmation; defaults to window.confirm. */
  confirmDiscard?(message: string): boolean;
  /** Editor factory seam (tests); production uses the lazy CM6 editor. */
  createEditor?: typeof createPaperSourceEditor;
}

const TEXT_SUFFIXES = new Set([
  '.tex', '.ltx', '.bib', '.bst', '.sty', '.cls', '.clo', '.cfg', '.def',
  '.fd', '.pgf', '.tikz', '.csv', '.tsv', '.dat', '.txt', '.enc', '.map', '.afm',
]);
const PLAIN_SUFFIXES = new Set(['.csv', '.tsv', '.dat', '.txt']);
const suffix = (path: string): string => (/\.[^./]+$/.exec(path)?.[0] ?? '').toLowerCase();
const editorLanguage = (path: string): SourceEditorLanguage =>
  PLAIN_SUFFIXES.has(suffix(path)) ? 'text' : 'latex';

/* One live editor per drawer body container. Navigation (tree, another
 * file, drawer re-render) destroys it; the back affordance asks first when
 * the buffer is dirty. */
const activeEditors = new WeakMap<HTMLElement, PaperSourceEditor>();

function destroyActiveEditor(container: HTMLElement): void {
  activeEditors.get(container)?.destroy();
  activeEditors.delete(container);
}

/** Dirty-guarded teardown for the back-to-tree affordance. */
function confirmEditorDiscard(container: HTMLElement, ctx: SourceViewContext): boolean {
  const editor = activeEditors.get(container);
  if (!editor) return true;
  if (editor.dirty()) {
    const ask = ctx.confirmDiscard ?? ((message: string) => window.confirm(message));
    if (!ask(ctx.tr('paperEdit.unsavedConfirm'))) return false;
  }
  destroyActiveEditor(container);
  return true;
}

async function fetchManifest(ctx: SourceViewContext, signal: AbortSignal): Promise<string[]> {
  const url = paperBuildArtifactPath(ctx.documentId, ctx.buildId, 'sources.json');
  const buffer = await ctx.api.bytes(url, signal);
  const rows = JSON.parse(new TextDecoder().decode(buffer)) as Array<{ path?: unknown }>;
  return rows.map(row => (typeof row?.path === 'string' ? row.path : ''))
    .filter(Boolean).sort();
}

function manifestTree(paths: string[]): Map<string, string[]> {
  const tree = new Map<string, string[]>();
  for (const path of paths) {
    const slash = path.lastIndexOf('/');
    const dir = slash < 0 ? '' : path.slice(0, slash);
    if (!tree.has(dir)) tree.set(dir, []);
    tree.get(dir)!.push(path);
  }
  return tree;
}

export async function showSourceTree(container: HTMLElement, ctx: SourceViewContext,
  signal: AbortSignal): Promise<void> {
  destroyActiveEditor(container);
  const list = document.createElement('div'); list.className = 'paper-source-tree';
  list.textContent = ctx.tr('common.loading');
  container.replaceChildren(list);
  let paths: string[];
  try {
    paths = await fetchManifest(ctx, signal);
  } catch (error) {
    if (signal.aborted) return;
    list.textContent = apiFailure(error).message;
    return;
  }
  if (signal.aborted) return;
  list.replaceChildren();
  if (!paths.length) {
    list.textContent = ctx.tr('preview.sourceEmpty');
    return;
  }
  for (const [dir, files] of [...manifestTree(paths).entries()].sort()) {
    if (dir) {
      const header = document.createElement('div'); header.className = 'paper-source-tree-dir';
      header.textContent = dir + '/'; list.append(header);
    }
    for (const path of files) {
      const item = document.createElement('button'); item.type = 'button';
      item.className = 'paper-source-tree-file';
      item.dataset.path = path;
      item.textContent = dir ? path.slice(dir.length + 1) : path;
      if (!TEXT_SUFFIXES.has(suffix(path))) {
        item.disabled = true;
        item.title = ctx.tr('preview.sourceBinary');
      } else {
        item.onclick = () => ctx.showFile(path);
      }
      list.append(item);
    }
  }
}

function renderSourceLines(pre: HTMLElement, content: string, language: string): void {
  const lines = content.split('\n');
  // A trailing newline would paint one phantom empty row; content stays untouched.
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  const fragment = document.createDocumentFragment();
  lines.forEach((text, index) => {
    const row = document.createElement('span');
    row.className = 'paper-source-line';
    row.dataset.line = String(index + 1);
    const gutter = document.createElement('span');
    gutter.className = 'paper-source-lineno';
    gutter.textContent = String(index + 1);
    const code = document.createElement('code');
    code.textContent = text === '' ? ' ' : text;
    row.append(gutter, code);
    fragment.append(row);
  });
  pre.replaceChildren(fragment);
  void ensureHljs(language).then(grammar => {
    if (!grammar || !pre.isConnected) return;
    pre.querySelectorAll('code').forEach(code => {
      code.innerHTML = grammar.highlight(code.textContent ?? '', { language }).value;
      code.classList.add('hljs');
    });
  });
}

async function ensureHljs(language: string) {
  try {
    const vendor = await import('../../vendor-runtime');
    return (await vendor.ensureHljsLanguage(language)) ? vendor.hljs : null;
  } catch { return null; }
}

function hitReadOnlyLine(pre: HTMLElement, line: number | undefined): void {
  if (!line || line < 1) return;
  const target = pre.querySelector<HTMLElement>(`[data-line="${line}"]`);
  if (target) {
    target.classList.add('paper-source-line-hit');
    target.scrollIntoView({ block: 'center' });
    window.setTimeout(() => target.classList.remove('paper-source-line-hit'), 2600);
  }
}

export async function showSourceFile(container: HTMLElement, ctx: SourceViewContext,
  path: string, line: number | undefined, signal: AbortSignal): Promise<void> {
  destroyActiveEditor(container);
  const view = document.createElement('div'); view.className = 'paper-source-view';
  const header = document.createElement('div'); header.className = 'paper-source-view-header';
  const back = document.createElement('button'); back.type = 'button';
  back.className = 'paper-source-back';
  back.textContent = '‹ ' + ctx.tr('preview.sources');
  back.onclick = () => { if (confirmEditorDiscard(container, ctx)) ctx.showTree(); };
  const name = document.createElement('span'); name.className = 'paper-source-view-path';
  name.textContent = path;
  header.append(back, name);
  const pre = document.createElement('pre'); pre.className = 'paper-source-code';
  pre.textContent = ctx.tr('common.loading');
  view.append(header, pre);
  container.replaceChildren(view);
  try {
    const source = await ctx.documentsApi.readSource(
      { document_id: ctx.documentId, path }, signal);
    if (signal.aborted) return;
    // The read-only render is also the editor's loading/failure fallback:
    // paint it first, swap in CodeMirror only when editing is safe (text
    // suffix, full untruncated read) and the lazy bundle actually loaded.
    renderSourceLines(pre, source.content, 'latex');
    const editable = TEXT_SUFFIXES.has(suffix(path)) && !source.truncated;
    if (!editable) {
      hitReadOnlyLine(pre, line);
      back.focus({ preventScroll: true });
      return;
    }
    const makeEditor = ctx.createEditor ?? createPaperSourceEditor;
    const editor = await makeEditor({
      path,
      content: source.content,
      language: editorLanguage(path),
      tr: ctx.tr,
      signal,
      save: (request) => ctx.documentsApi.editSource({
        document_id: ctx.documentId,
        path,
        content: request.content,
        ...(request.baseSha256 ? { base_sha256: request.baseSha256 } : {}),
      }, signal),
      reload: () => { void showSourceFile(container, ctx, path, line, signal); },
      submitChat: ctx.submitChat,
      onSaved: ctx.onSourceSaved,
    });
    if (signal.aborted) { editor?.destroy(); return; }
    if (!editor) {
      hitReadOnlyLine(pre, line);
      back.focus({ preventScroll: true });
      return;
    }
    activeEditors.set(container, editor);
    pre.replaceWith(editor.element);
    if (line && line >= 1) editor.scrollToLine(line);
    editor.focus();
  } catch (error) {
    if (signal.aborted) return;
    pre.textContent = apiFailure(error).message;
  }
}
