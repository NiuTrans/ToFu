/** Margin notes bound to one source and canonical document language.
 * Owns list projection and one modal editor; repositories own durable notes. */
import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import { createLifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';
import { createButton, createDialog, createField, createAlert, setFieldError, type DialogHandle } from '../../ui';

type ViewKey = '_paperNotes' | '_paperNotesLang';

interface NoteAnchor {
  heading_idx?: number | null;
  char_offset?: number | null;
  quote?: string;
}

interface PaperNote {
  id: string;
  note: string;
  anchor?: NoteAnchor;
  [key: string]: unknown;
}

interface PaperNotesView {
  kind?: string;
  containerId?: string;
  meta?: unknown;
  langKey?: () => string;
  _paperNotes?: PaperNote[];
  _paperNotesLang?: string;
}

interface PaperNotesApi {
  notesList(paperHash: string, lang: string): Promise<Record<string, unknown>>;
  notesCreate(body: Record<string, unknown>): Promise<Record<string, unknown>>;
  notesUpdate(noteId: string, text: string): Promise<unknown>;
  notesDelete(noteId: string): Promise<unknown>;
}

interface NoteEditor {
  element: HTMLElement;
  noteId: string | null;
  anchor: NoteAnchor;
  dialog: DialogHandle;
}

type PaperNotesWindow = Window & {
  Api?: { paper?: PaperNotesApi };
  t?: (key: string) => string;
  _reportView?: (kind: string) => PaperNotesView;
  _paperHash?: string;
  _activePaperId?: string;
  _paperActiveTab?: string;
  _paperXpGet?: <T>(view: PaperNotesView, key: ViewKey) => T | undefined;
  _paperXpSet?: (view: PaperNotesView, key: ViewKey, value: unknown) => void;
  _deepenReportHeadings?: (article: Element) => ArrayLike<Element>;
  _paperAskQuestion?: (question: string) => void;
  _hidePaperQuoteBar?: () => void;
  _paperNotesClickWired?: boolean;
  __tofuPaperNotesOwned?: boolean;
  __tofuPaperNotesDestroy?: () => void;
  _paperNoteFromSelection?: () => void;
  _paperNotesAfterRender?: (
    article: Element,
    container: Element | null,
    view: PaperNotesView,
  ) => void;
  _paperNotesDecorate?: (article: Element, view: PaperNotesView) => void;
  _paperNoteOpenEditor?: (
    anchor: NoteAnchor,
    existing: PaperNote | null,
    x: number,
    y: number,
  ) => void;
  _paperNoteAnchorFromSelection?: () => NoteAnchor | null;
};

function globals(): PaperNotesWindow {
  return featureRegistry as unknown as PaperNotesWindow;
}

function translate(key: I18nKey): string {
  const fn = globals().t;
  return typeof fn === 'function' ? fn(key) : key;
}

function api(): PaperNotesApi {
  const paper = globals().Api?.paper;
  if (!paper) throw new Error('Paper notes API unavailable');
  return paper;
}

let fallbackView: PaperNotesView | null = null;
let editor: NoteEditor | null = null;
let notesEpoch = 0;
const noteSources = new WeakMap<PaperNotesView, { key: string; notes?: WeakRef<PaperNote[]> }>();
const noteRevisions = new Map<string, number>();
let revisionSource = '';
function noteRevision(view: PaperNotesView, advance = false): number {
  const source = JSON.stringify([globals()._activePaperId || '', globals()._paperHash || '', notesEpoch]);
  if (revisionSource !== source) { noteRevisions.clear(); revisionSource = source; }
  const key = view.langKey?.() || '', value = (noteRevisions.get(key) || 0) + (advance ? 1 : 0);
  if (advance) noteRevisions.set(key, value); return value;
}
function sourceKey(view: PaperNotesView): string { return JSON.stringify([globals()._activePaperId || '', globals()._paperHash || '', view.langKey?.() || '']); }
function bindNotes(view: PaperNotesView): void {
  const key = sourceKey(view), previous = noteSources.get(view);
  const notes = viewGet<PaperNote[]>(view, '_paperNotes');
  if (previous && previous.key !== key && notes === previous.notes?.deref()) { viewSet(view, '_paperNotes', undefined); viewSet(view, '_paperNotesLang', undefined); }
  const adopted = viewGet<PaperNote[]>(view, '_paperNotes');
  noteSources.set(view, { key, notes: Array.isArray(adopted) ? new WeakRef(adopted) : undefined });
}
function sourceFence(view: PaperNotesView): () => boolean {
  const key = sourceKey(view), epoch = notesEpoch;
  return () => notesEpoch === epoch && sourceKey(view) === key;
}

function notesView(context?: Element | null): PaperNotesView {
  const reportView = globals()._reportView;
  const host = context?.closest('#paperReportContent, #paperReviewContent, #paperRebuttalContent');
  const kind = host?.id === 'paperRebuttalContent' ? 'rebuttal' : host?.id === 'paperReviewContent' ? 'review' : host?.id === 'paperReportContent' ? 'report' : ['review', 'rebuttal'].includes(globals()._paperActiveTab || '') ? globals()._paperActiveTab! : 'report';
  if (typeof reportView === 'function') return reportView(kind);
  fallbackView ??= {
    kind: 'report',
    containerId: 'paperReportContent',
    meta: null,
    langKey: () => 'en',
  };
  return fallbackView;
}

function viewGet<T>(view: PaperNotesView, key: ViewKey): T | undefined {
  const get = globals()._paperXpGet;
  if (typeof get === 'function') return get<T>(view, key);
  return view[key] as T | undefined;
}

function viewSet(view: PaperNotesView, key: ViewKey, value: unknown): void {
  const set = globals()._paperXpSet;
  if (typeof set === 'function') set(view, key, value);
  else (view as Record<ViewKey, unknown>)[key] = value;
  if (key === '_paperNotes') noteSources.set(view, { key: sourceKey(view), notes: Array.isArray(value) ? new WeakRef(value) : undefined });
}

async function loadNotes(view: PaperNotesView | null): Promise<void> {
  if (!view) return;
  bindNotes(view);
  const current = sourceFence(view);
  const lang = view.langKey?.() ?? '';
  const paperHash = globals()._paperHash ?? '';
  if (!paperHash || !lang) return;
  if (viewGet<string>(view, '_paperNotesLang') === lang
      && Array.isArray(viewGet(view, '_paperNotes'))) return;
  const revision = noteRevision(view, true);
  try {
    const data = await api().notesList(paperHash, lang);
    if (!current() || noteRevision(view) !== revision) return;
    if (!data || data.ok !== true || !Array.isArray(data.notes)) throw new Error(translate('paper.noteLoadFailed'));
    if (data.ok === true && Array.isArray(data.notes)) {
      viewSet(view, '_paperNotes', data.notes);
      viewSet(view, '_paperNotesLang', lang);
    }
  } catch (error: unknown) {
    if (!current() || noteRevision(view) !== revision) return;
    const article = document.getElementById(view.containerId || 'paperReportContent')?.querySelector('.paper-report-article');
    if (article && !article.querySelector('.paper-notes-feedback')) {
      const feedback = createAlert({ variant: 'warning', title: translate('paper.noteLoadFailed') });
      feedback.el.classList.add('paper-notes-feedback'); article.append(feedback.el);
    }
    console.debug('[Paper:Notes] load failed (non-fatal):', error);
  }
}

function reportHeadings(article: Element): Element[] {
  const shared = globals()._deepenReportHeadings;
  return Array.from(typeof shared === 'function'
    ? shared(article)
    : article.querySelectorAll('h2, h3'));
}

function findQuote(
  article: Element,
  quote: string,
): { node: Node; index: number; length: number } | null {
  const needle = quote.replace(/\s+/g, ' ').trim().slice(0, 80);
  if (!needle) return null;
  const walker = document.createTreeWalker(article, 4, {
    acceptNode(node: Node): number {
      if (!node.nodeValue || !/\S/.test(node.nodeValue)) return 2;
      const tag = (node.parentNode as Element | null)?.tagName ?? '';
      return /^(SCRIPT|STYLE|TEXTAREA|BUTTON)$/.test(tag) ? 2 : 1;
    },
  });
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const index = (node.nodeValue ?? '').replace(/\s+/g, ' ').indexOf(needle);
    if (index >= 0) return { node, index, length: needle.length };
  }
  return null;
}

function noteChip(note: PaperNote): string {
  return `<button type="button" class="paper-note-chip" data-note-id="${escape(note.id)}" title="${escape(note.note)}">📝</button>`;
}

export function decoratePaperNotes(article: Element, view: PaperNotesView): void {
  if (!article || !view) return;
  article.querySelectorAll('.paper-note-mark, .paper-note-chip, .paper-note-tray')
    .forEach((node) => {
      if (node.classList.contains('paper-note-mark')) {
        const parent = node.parentNode;
        if (!parent) return;
        while (node.firstChild) parent.insertBefore(node.firstChild, node);
        parent.removeChild(node);
      } else {
        node.parentNode?.removeChild(node);
      }
    });
  const notes = viewGet<PaperNote[]>(view, '_paperNotes') ?? [];
  if (!notes.length) return;
  const headings = reportHeadings(article);
  const orphans: PaperNote[] = [];
  for (const note of notes) {
    const anchor = note.anchor ?? {};
    let hit = findQuote(article, anchor.quote ?? '');
    if (hit) {
      try {
        const range = document.createRange();
        range.setStart(hit.node, hit.index);
        range.setEnd(
          hit.node,
          Math.min(hit.node.nodeValue?.length ?? 0, hit.index + hit.length),
        );
        const mark = document.createElement('span');
        mark.className = 'paper-note-mark';
        mark.dataset.noteId = note.id;
        mark.title = note.note;
        range.surroundContents(mark);
      } catch (error: unknown) {
        console.debug('[Paper:Notes] quote anchor fallback:', error);
        hit = null;
      }
    }
    if (hit) continue;
    const index = typeof anchor.heading_idx === 'number'
      ? anchor.heading_idx
      : null;
    if (index != null && headings[index]) {
      headings[index].insertAdjacentHTML('beforeend', noteChip(note));
    } else {
      orphans.push(note);
    }
  }
  if (!orphans.length) return;
  const tray = document.createElement('div');
  tray.className = 'paper-note-tray';
  tray.innerHTML = `<div class="paper-note-tray-head">📝 ${escape(translate('paper.noteOrphans'))}</div>`;
  for (const note of orphans) {
    const row = document.createElement('div');
    row.className = 'paper-note-tray-row';
    row.dataset.noteId = note.id;
    row.textContent = note.note;
    tray.appendChild(row);
  }
  article.appendChild(tray);
}

export function noteAnchorFromSelection(): NoteAnchor | null {
  const selection = window.getSelection();
  const quote = selection?.toString().trim() ?? '';
  if (!quote) return null;
  const article = document.getElementById(notesView(selection?.anchorNode?.parentElement).containerId || 'paperReportContent')
    ?.querySelector('.paper-report-article');
  if (!article || !selection?.anchorNode || !article.contains(selection.anchorNode)
      || !selection.focusNode || !article.contains(selection.focusNode)) return null;
  const headings = reportHeadings(article);
  let headingIndex: number | null = null;
  const anchorNode = selection?.anchorNode;
  const element = anchorNode?.nodeType === 1
    ? anchorNode as Element
    : anchorNode?.parentElement;
  if (element) {
    for (let index = 0; index < headings.length; index += 1) {
      const heading = headings[index];
      const relation = heading.compareDocumentPosition(element);
      if ((relation & 4) || heading.contains(element) || heading === element) {
        headingIndex = index;
      } else break;
    }
  }
  return {
    heading_idx: headingIndex,
    char_offset: null,
    quote: quote.slice(0, 400),
  };
}

export function closePaperNoteEditor(): void {
  const previous = editor; editor = null;
  previous?.dialog.close(); previous?.element.remove();
}

function refreshNotes(view: PaperNotesView): void {
  const article = document.getElementById(view.containerId || 'paperReportContent')
    ?.querySelector('.paper-report-article');
  if (article) decoratePaperNotes(article, view);
}

export function openPaperNoteEditor(
  anchor: NoteAnchor,
  existing: PaperNote | null,
  _x: number,
  _y: number,
  ownerView?: PaperNotesView,
): void {
  closePaperNoteEditor();
  const view = ownerView || notesView(); bindNotes(view);
  const sourceCurrent = sourceFence(view), paperHash = globals()._paperHash || '', lang = view.langKey?.() || '';
  const body = document.createElement('div'); body.className = 'paper-note-form';
  if (anchor.quote) { const quote = document.createElement('blockquote'); quote.className = 'paper-note-form-quote'; quote.textContent = anchor.quote.slice(0, 400); body.append(quote); }
  const input = document.createElement('textarea'); input.className = 'paper-note-editor-input'; input.rows = 5; input.placeholder = translate('paper.notePlaceholder'); input.value = existing?.note || '';
  const field = createField({ label: translate('paper.noteBtn'), control: input }); body.append(field.el);
  const actions = document.createElement('div'); actions.className = 'paper-note-form-actions'; body.append(actions);
  const dialog = createDialog({ title: translate('paper.noteBtn'), body, onClose: () => {
    if (editor?.dialog === dialog) { editor = null; dialog.el.remove(); }
  } });
  const current: NoteEditor = { element: dialog.el, dialog, noteId: existing?.id || null, anchor: existing?.anchor || anchor };
  dialog.el.classList.add('paper-note-editor');
  editor = current;
  const isCurrent = (): boolean => editor === current && sourceCurrent();
  let busy = false;
  const mutate = async (remove: boolean): Promise<void> => {
    if (!isCurrent() || busy) return;
    const text = input.value.trim(); if (!remove && !text) return;
    const reloadNeeded = !Array.isArray(viewGet(view, '_paperNotes'));
    noteRevision(view, true);
    busy = true; save.disabled = true; if (del) del.disabled = true; setFieldError(field, null);
    try {
      if (remove && current.noteId) {
        const response = await api().notesDelete(current.noteId) as Record<string, unknown>;
        if (response?.ok !== true) throw new Error(String(response?.error || translate('paper.noteMutationFailed')));
        if (!isCurrent()) return;
        noteRevision(view, true);
        viewSet(view, '_paperNotes', (viewGet<PaperNote[]>(view, '_paperNotes') || []).filter(note => note.id !== current.noteId));
      } else if (current.noteId) {
        const response = await api().notesUpdate(current.noteId, text) as Record<string, unknown>;
        if (response?.ok !== true) throw new Error(String(response?.error || translate('paper.noteMutationFailed')));
        if (!isCurrent()) return;
        noteRevision(view, true);
        viewSet(view, '_paperNotes', (viewGet<PaperNote[]>(view, '_paperNotes') || []).map(note => note.id === current.noteId ? { ...note, note: text } : note));
      } else {
        const response = await api().notesCreate({ paper_hash: paperHash, lang, anchor: current.anchor, note: text });
        if (response.ok !== true || !response.note || typeof response.note !== 'object') throw new Error(String(response?.error || translate('paper.noteMutationFailed')));
        if (!isCurrent()) return;
        noteRevision(view, true);
        viewSet(view, '_paperNotes', (viewGet<PaperNote[]>(view, '_paperNotes') || []).concat([response.note as PaperNote]));
      }
      closePaperNoteEditor(); refreshNotes(view);
      if (reloadNeeded) void loadNotes(view).then(() => { if (sourceCurrent()) refreshNotes(view); });
    } catch (error: unknown) {
      if (isCurrent()) setFieldError(field, error instanceof Error ? error.message : translate('paper.noteMutationFailed'));
    } finally { if (isCurrent()) { busy = false; save.disabled = false; if (del) del.disabled = false; } }
  };
  const save = createButton({ label: translate('paper.noteSave'), variant: 'primary', onClick: () => { void mutate(false); } }); save.classList.add('paper-note-save'); actions.append(save);
  let del: HTMLButtonElement | null = null;
  if (existing) {
    const ask = createButton({ label: translate('paper.noteAsk'), onClick: () => {
      if (!isCurrent()) return;
      const question = (current.anchor.quote ? `> ${current.anchor.quote}\n\n` : '') + input.value;
      closePaperNoteEditor(); globals()._paperAskQuestion?.(question);
    } }); ask.classList.add('paper-note-ask'); actions.append(ask);
    del = createButton({ label: translate('paper.noteDelete'), variant: 'danger', onClick: () => { void mutate(true); } }); del.classList.add('paper-note-del'); actions.append(del);
  }
  const cancel = createButton({ label: translate('paper.noteCancel'), onClick: closePaperNoteEditor }); cancel.classList.add('paper-note-cancel'); actions.append(cancel);
  document.body.append(dialog.el); void dialog.open(); input.focus();
}

export function noteFromSelection(): void {
  const anchor = noteAnchorFromSelection();
  if (!anchor) return;
  const selection = window.getSelection();
  const rect = selection?.rangeCount
    ? selection.getRangeAt(0).getBoundingClientRect()
    : { left: 200, bottom: 200 };
  openPaperNoteEditor(anchor, null, rect.left, rect.bottom, notesView(selection?.anchorNode?.parentElement));
  globals()._hidePaperQuoteBar?.();
}

export function notesAfterRender(
  article: Element,
  _container: Element | null,
  view: PaperNotesView,
): void {
  if (!view) return;
  bindNotes(view);
  const sourceCurrent = sourceFence(view);
  const current = viewGet<PaperNote[]>(view, '_paperNotes');
  if (Array.isArray(current)) decoratePaperNotes(article, view);
  else void loadNotes(view).then(() => { if (sourceCurrent() && article.isConnected) decoratePaperNotes(article, view); });
}

export function attachPaperNotes(): () => void {
  const state = globals();
  if (state.__tofuPaperNotesOwned && state.__tofuPaperNotesDestroy) {
    return state.__tofuPaperNotesDestroy;
  }
  const scope = createLifecycleScope();
  state.__tofuPaperNotesOwned = true;
  state._paperNotesClickWired = true;
  scope.listen(document, 'click', (event) => {
    const target = event.target as { closest?: (selector: string) => Element | null } | null;
    const chip = target?.closest?.(
      '.paper-note-chip, .paper-note-mark, .paper-note-tray-row');
    if (!chip) return;
    const id = chip.getAttribute('data-note-id');
    const view = notesView(chip);
    const note = (viewGet<PaperNote[]>(view, '_paperNotes') ?? [])
      .find((candidate) => candidate.id === id);
    if (!note) return;
    const rect = chip.getBoundingClientRect();
    openPaperNoteEditor(note.anchor ?? {}, note, rect.left, rect.bottom, view);
  });
  scope.listen(document, 'keydown', (event) => {
    if ((event as KeyboardEvent).key === 'Escape' && editor) {
      closePaperNoteEditor();
    }
  });
  const destroy = (): void => {
    notesEpoch += 1;
    scope.destroy();
    closePaperNoteEditor();
    if (state.__tofuPaperNotesDestroy === destroy) {
      state.__tofuPaperNotesDestroy = undefined;
      state.__tofuPaperNotesOwned = false;
      state._paperNotesClickWired = false;
    }
  };
  state.__tofuPaperNotesDestroy = destroy;
  return destroy;
}

const state = globals();
(state as PaperNotesWindow & { _cancelPaperNotes: () => void })._cancelPaperNotes = () => { notesEpoch += 1; closePaperNoteEditor(); };
state._paperNoteFromSelection = noteFromSelection;
state._paperNotesAfterRender = notesAfterRender;
state._paperNotesDecorate = decoratePaperNotes;
state._paperNoteOpenEditor = openPaperNoteEditor;
state._paperNoteAnchorFromSelection = noteAnchorFromSelection;
attachPaperNotes();
