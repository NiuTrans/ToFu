/** Track B: editable CodeMirror 6 source view for the paper preview drawer.
 *
 * Responsibility: own the editor chrome (save affordance, dirty marker,
 * external-modification conflict banner, selection bubble menu) around a
 * pluggable editor adapter. The heavy CodeMirror bundle loads lazily through
 * codemirror-loader; when it cannot load, `createPaperSourceEditor` resolves
 * to null and the caller keeps the read-only code view untouched. Save flow:
 * POST via the injected `save` port with the sha256 of the last loaded bytes
 * as an optimistic-concurrency fence; a 'source_conflict' failure offers
 * reload (server bytes win) or force save (no fence). */
import { apiFailure } from '../../api/errors';
import type { Translator } from '../../i18n';
import { ensureCodeMirror, type CodeMirrorBundle } from './codemirror-loader';

export type SourceEditorLanguage = 'latex' | 'text';

export interface SourceEditorSelection {
  text: string;
  fromLine: number;
  toLine: number;
  /** Viewport coordinates of the selection head, for bubble placement. */
  rect: { left: number; top: number; bottom: number };
}

/** Minimal editor surface the chrome drives. CodeMirror is one adapter;
 * tests inject a textarea-sized fake so every chrome behavior (conflict
 * banner, dirty lifecycle, bubble message) is exercised in node/jsdom. */
export interface SourceEditorAdapter {
  readonly element: HTMLElement;
  content(): string;
  onDidChange(listener: () => void): void;
  onSelectionChange(listener: (selection: SourceEditorSelection | null) => void): void;
  setSaveCommand(command: (() => void) | null): void;
  scrollToLine(line: number): void;
  focus(): void;
  destroy(): void;
}

export interface SourceEditorAdapterInit {
  content: string;
  language: SourceEditorLanguage;
}
export type SourceEditorAdapterFactory =
  (init: SourceEditorAdapterInit) => SourceEditorAdapter | Promise<SourceEditorAdapter>;

export interface SourceEditorSaveRequest {
  content: string;
  baseSha256?: string;
}
export interface SourceEditorSaveResult {
  sha256: string;
  size_bytes?: number;
}

export interface PaperSourceEditorOptions {
  path: string;
  content: string;
  language?: SourceEditorLanguage;
  tr: Translator;
  signal?: AbortSignal;
  save(request: SourceEditorSaveRequest): Promise<SourceEditorSaveResult>;
  /** Re-read the file from the server (conflict recovery / external change). */
  reload(): void;
  /** Prefill + send one main-composer chat message (bubble menu actions). */
  submitChat?(message: string): void;
  /** Successful save hook — the panel nudges the same compile flow here. */
  onSaved?(): void;
  onDirtyChange?(dirty: boolean): void;
  /** Injectable seams for node tests; production uses the lazy CM6 bundle. */
  loadModules?: () => Promise<CodeMirrorBundle>;
  digest?: (text: string) => Promise<string>;
  createAdapter?: SourceEditorAdapterFactory;
}

export interface PaperSourceEditor {
  readonly element: HTMLElement;
  dirty(): boolean;
  conflictOpen(): boolean;
  scrollToLine(line: number): void;
  save(force?: boolean): Promise<void>;
  focus(): void;
  destroy(): void;
}

export async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

/** Chat message the selection bubble sends: instruction + quoted selection
 * + full-width source context `（path:from-to）` (the agent parses this
 * shape for its journaled edit tools). */
export function buildBubbleMessage(input: {
  instruction: string;
  selectedText: string;
  path: string;
  fromLine: number;
  toLine: number;
}): string {
  const text = input.selectedText.trim();
  return `${input.instruction}\n\n${text}\n\n（${input.path}:${input.fromLine}-${input.toLine}）`;
}

const BUBBLE_ACTIONS: ReadonlyArray<Readonly<{
  labelKey: 'paperEdit.bubble.academize' | 'paperEdit.bubble.concise' | 'paperEdit.bubble.expand'
    | 'paperEdit.bubble.fixGrammar' | 'paperEdit.bubble.explain';
  instructionKey: 'paperEdit.instruct.academize' | 'paperEdit.instruct.concise' | 'paperEdit.instruct.expand'
    | 'paperEdit.instruct.fixGrammar' | 'paperEdit.instruct.explain';
}>> = Object.freeze([
  { labelKey: 'paperEdit.bubble.academize', instructionKey: 'paperEdit.instruct.academize' },
  { labelKey: 'paperEdit.bubble.concise', instructionKey: 'paperEdit.instruct.concise' },
  { labelKey: 'paperEdit.bubble.expand', instructionKey: 'paperEdit.instruct.expand' },
  { labelKey: 'paperEdit.bubble.fixGrammar', instructionKey: 'paperEdit.instruct.fixGrammar' },
  { labelKey: 'paperEdit.bubble.explain', instructionKey: 'paperEdit.instruct.explain' },
]);

function createCodeMirrorAdapter(bundle: CodeMirrorBundle, init: SourceEditorAdapterInit): SourceEditorAdapter {
  /* CM6's DOM observer/measure loop needs these host globals at construction
   * and paints asynchronously afterwards. Probe BEFORE `new EditorView`: a
   * half-constructed view would still schedule its measure loop and throw
   * outside our catch. Every real browser passes; a headless/test DOM that
   * lacks them falls back to the read-only view whole. */
  if (typeof MutationObserver !== 'function' || typeof getComputedStyle !== 'function') {
    throw new Error('CodeMirror host environment unavailable');
  }
  const {
    EditorState, EditorView, keymap, lineNumbers, highlightActiveLine,
    highlightActiveLineGutter, drawSelection, history, defaultKeymap,
    historyKeymap, indentWithTab, search, searchKeymap,
    highlightSelectionMatches, autocompletion, closeBrackets, StreamLanguage,
    syntaxHighlighting, defaultHighlightStyle, bracketMatching, stex,
  } = bundle;
  let changeListener: (() => void) | null = null;
  let selectionListener: ((selection: SourceEditorSelection | null) => void) | null = null;
  let saveCommand: (() => void) | null = null;
  const theme = EditorView.theme({
    '&': {
      backgroundColor: 'transparent', color: 'var(--text-primary)',
      fontSize: 'var(--font-size-xs)', maxHeight: '60vh',
    },
    '.cm-content': {
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      caretColor: 'var(--accent)',
    },
    '.cm-scroller': { font: 'inherit', lineHeight: '1.55' },
    '.cm-gutters': {
      backgroundColor: 'transparent', color: 'var(--text-secondary)',
      border: 'none',
    },
    '.cm-activeLine': { backgroundColor: 'var(--bg-hover)' },
    '.cm-activeLineGutter': { backgroundColor: 'var(--bg-hover)' },
    '&.cm-focused': { outline: 'none' },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
      backgroundColor: 'var(--accent-subtle)',
    },
    '.cm-panels': {
      backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)',
    },
    '.cm-searchMatch': { backgroundColor: 'var(--accent-subtle)' },
  });
  const view = new EditorView({
    parent: document.createElement('div'),
    state: EditorState.create({
      doc: init.content,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        drawSelection(),
        history(),
        bracketMatching(),
        closeBrackets(),
        autocompletion(),
        search({ top: true }),
        highlightSelectionMatches(),
        init.language === 'latex'
          ? [StreamLanguage.define(stex), syntaxHighlighting(defaultHighlightStyle, { fallback: true })]
          : [],
        theme,
        keymap.of([
          {
            key: 'Mod-s',
            preventDefault: true,
            run: () => { saveCommand?.(); return true; },
          },
          ...defaultKeymap,
          ...historyKeymap,
          ...searchKeymap,
          indentWithTab,
        ]),
        EditorView.updateListener.of(update => {
          if (update.docChanged) changeListener?.();
          if (update.selectionSet && selectionListener) {
            const range = update.state.selection.main;
            if (range.empty) {
              selectionListener(null);
              return;
            }
            const head = view.coordsAtPos(range.head);
            if (!head) {
              selectionListener(null);
              return;
            }
            selectionListener({
              text: update.state.sliceDoc(range.from, range.to),
              fromLine: update.state.doc.lineAt(range.from).number,
              toLine: update.state.doc.lineAt(range.to).number,
              rect: { left: head.left, top: head.top, bottom: head.bottom },
            });
          }
        }),
      ],
    }),
  });
  return {
    element: view.dom,
    content: () => view.state.doc.toString(),
    onDidChange: listener => { changeListener = listener; },
    onSelectionChange: listener => { selectionListener = listener; },
    setSaveCommand: command => { saveCommand = command; },
    scrollToLine: line => {
      const target = Math.max(1, Math.min(line, view.state.doc.lines));
      const position = view.state.doc.line(target).from;
      view.dispatch({
        selection: { anchor: position },
        effects: EditorView.scrollIntoView(position, { y: 'center' }),
      });
    },
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

export async function createPaperSourceEditor(
  options: PaperSourceEditorOptions,
): Promise<PaperSourceEditor | null> {
  const tr = options.tr;
  const digest = options.digest ?? sha256Hex;
  const factory: SourceEditorAdapterFactory = options.createAdapter
    ?? (async init => createCodeMirrorAdapter(await (options.loadModules ?? ensureCodeMirror)(), init));

  let adapter: SourceEditorAdapter;
  try {
    adapter = await factory({
      content: options.content,
      language: options.language ?? 'latex',
    });
  } catch {
    // Lazy bundle unavailable: the caller's read-only view stays authoritative.
    return null;
  }
  if (options.signal?.aborted) {
    adapter.destroy();
    return null;
  }

  const element = document.createElement('div');
  element.className = 'paper-source-editor';

  const toolbar = document.createElement('div');
  toolbar.className = 'paper-source-editor-toolbar';
  const dirtyBadge = document.createElement('span');
  dirtyBadge.className = 'paper-source-editor-dirty';
  dirtyBadge.textContent = tr('paperEdit.dirty');
  dirtyBadge.hidden = true;
  const status = document.createElement('span');
  status.className = 'paper-source-editor-status';
  status.setAttribute('role', 'status');
  const saveButton = document.createElement('button');
  saveButton.type = 'button';
  saveButton.className = 'paper-source-editor-save';
  saveButton.textContent = tr('paperEdit.save');
  saveButton.title = tr('paperEdit.save') + ' (Ctrl/⌘+S)';
  toolbar.append(dirtyBadge, status, saveButton);

  const conflict = document.createElement('div');
  conflict.className = 'paper-source-editor-conflict';
  conflict.hidden = true;
  conflict.setAttribute('role', 'alert');
  const conflictText = document.createElement('span');
  conflictText.className = 'paper-source-editor-conflict-text';
  conflictText.textContent = tr('paperEdit.conflict');
  const reloadButton = document.createElement('button');
  reloadButton.type = 'button';
  reloadButton.className = 'paper-source-editor-conflict-reload';
  reloadButton.textContent = tr('paperEdit.reload');
  const forceButton = document.createElement('button');
  forceButton.type = 'button';
  forceButton.className = 'paper-source-editor-conflict-force';
  forceButton.textContent = tr('paperEdit.forceSave');
  conflict.append(conflictText, reloadButton, forceButton);

  const host = document.createElement('div');
  host.className = 'paper-source-editor-host';
  host.append(adapter.element);

  const bubble = document.createElement('div');
  bubble.className = 'paper-source-bubble';
  bubble.hidden = true;
  bubble.setAttribute('role', 'toolbar');
  bubble.setAttribute('aria-label', tr('preview.sources'));
  const custom = document.createElement('input');
  custom.type = 'text';
  custom.className = 'paper-source-bubble-custom';
  custom.placeholder = tr('paperEdit.bubble.customPlaceholder');
  custom.setAttribute('aria-label', tr('paperEdit.bubble.customPlaceholder'));

  element.append(toolbar, conflict, host, bubble);

  let savedContent = options.content;
  let baseSha = '';
  let saving = false;
  let destroyed = false;
  let statusTimer: ReturnType<typeof setTimeout> | null = null;
  let selection: SourceEditorSelection | null = null;
  const digestReady = digest(options.content).then(sha => { baseSha = sha; })
    .catch(() => { baseSha = ''; });

  const isDirty = (): boolean => adapter.content() !== savedContent;
  const setStatus = (text: string, transient = false): void => {
    status.textContent = text;
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = null;
    if (transient && text) {
      statusTimer = setTimeout(() => {
        statusTimer = null;
        if (!destroyed) status.textContent = '';
      }, 2600);
    }
  };
  const syncDirty = (): void => {
    const value = isDirty();
    if (dirtyBadge.hidden === !value) return;
    dirtyBadge.hidden = !value;
    options.onDirtyChange?.(value);
  };
  const openConflict = (open: boolean): void => {
    conflict.hidden = !open;
    if (open) reloadButton.focus({ preventScroll: true });
  };

  async function save(force = false): Promise<void> {
    if (saving || destroyed) return;
    if (!isDirty() && !force) return;
    saving = true;
    saveButton.disabled = true;
    setStatus(tr('paperEdit.saving'));
    await digestReady;
    const content = adapter.content();
    try {
      const result = await options.save({
        content,
        ...(force ? {} : { baseSha256: baseSha }),
      });
      if (destroyed) return;
      savedContent = content;
      baseSha = result.sha256 || baseSha;
      openConflict(false);
      syncDirty();
      setStatus(tr('paperEdit.saved'), true);
      options.onSaved?.();
    } catch (error) {
      if (destroyed) return;
      const failure = apiFailure(error);
      if (failure.code === 'source_conflict') {
        openConflict(true);
        setStatus('');
      } else {
        setStatus(failure.message, true);
      }
    } finally {
      saving = false;
      if (!destroyed) saveButton.disabled = false;
    }
  }

  const hideBubble = (): void => {
    if (bubble.hidden) return;
    bubble.hidden = true;
    selection = null;
  };
  const submitSelection = (instruction: string): void => {
    const current = selection;
    if (!current || !instruction.trim()) return;
    const message = buildBubbleMessage({
      instruction: instruction.trim(),
      selectedText: current.text,
      path: options.path,
      fromLine: current.fromLine,
      toLine: current.toLine,
    });
    hideBubble();
    options.submitChat?.(message);
  };
  const positionBubble = (): void => {
    if (!selection) return;
    const hostRect = host.getBoundingClientRect();
    bubble.hidden = false;
    const bubbleRect = bubble.getBoundingClientRect();
    const left = Math.max(0, Math.min(
      selection.rect.left - hostRect.left,
      hostRect.width - bubbleRect.width,
    ));
    const above = selection.rect.top - hostRect.top - bubbleRect.height - 6;
    bubble.style.left = `${Math.round(left)}px`;
    bubble.style.top = `${Math.round(above >= 0 ? above
      : selection.rect.bottom - hostRect.top + 6)}px`;
  };
  for (const action of BUBBLE_ACTIONS) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'paper-source-bubble-action';
    button.textContent = tr(action.labelKey);
    button.addEventListener('click', () => submitSelection(tr(action.instructionKey)));
    bubble.append(button);
  }
  custom.addEventListener('keydown', event => {
    if (event.key === 'Enter' && custom.value.trim()) {
      event.preventDefault();
      submitSelection(custom.value);
      custom.value = '';
    }
  });
  bubble.append(custom);

  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape' && !bubble.hidden) {
      event.stopPropagation();
      hideBubble();
      adapter.focus();
    }
  };
  document.addEventListener('keydown', onKeydown, true);
  host.addEventListener('scroll', hideBubble);

  adapter.onDidChange(syncDirty);
  adapter.onSelectionChange(next => {
    selection = next && next.text.trim() ? next : null;
    if (selection) positionBubble();
    else hideBubble();
  });
  adapter.setSaveCommand(() => { void save(); });
  saveButton.addEventListener('click', () => { void save(); });
  reloadButton.addEventListener('click', () => options.reload());
  forceButton.addEventListener('click', () => { void save(true); });

  const handle: PaperSourceEditor = {
    element,
    dirty: isDirty,
    conflictOpen: () => !conflict.hidden,
    scrollToLine: line => adapter.scrollToLine(line),
    save,
    focus: () => adapter.focus(),
    destroy: () => {
      if (destroyed) return;
      destroyed = true;
      if (statusTimer) clearTimeout(statusTimer);
      document.removeEventListener('keydown', onKeydown, true);
      adapter.setSaveCommand(null);
      adapter.destroy();
      element.remove();
    },
  };
  if (options.signal) {
    if (options.signal.aborted) handle.destroy();
    else options.signal.addEventListener('abort', () => handle.destroy(), { once: true });
  }
  syncDirty();
  return handle;
}
