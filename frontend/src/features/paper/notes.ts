import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';

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
}

type PaperNotesWindow = Window & {
  Api?: { paper?: PaperNotesApi };
  t?: (key: string) => string;
  escapeHtml?: (value: unknown) => string;
  _reportView?: (kind: string) => PaperNotesView;
  _paperHash?: string;
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

function escape(value: unknown): string {
  const fn = globals().escapeHtml;
  if (typeof fn === 'function') return fn(value);
  const span = document.createElement('span');
  span.textContent = value == null ? '' : String(value);
  return span.innerHTML;
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

function notesView(): PaperNotesView {
  const reportView = globals()._reportView;
  if (typeof reportView === 'function') return reportView('report');
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
}

async function loadNotes(view: PaperNotesView | null): Promise<void> {
  if (!view) return;
  const lang = view.langKey?.() ?? '';
  const paperHash = globals()._paperHash ?? '';
  if (!paperHash || !lang) return;
  if (viewGet<string>(view, '_paperNotesLang') === lang
      && Array.isArray(viewGet(view, '_paperNotes'))) return;
  try {
    const data = await api().notesList(paperHash, lang);
    if (data.ok === true && Array.isArray(data.notes)) {
      viewSet(view, '_paperNotes', data.notes);
      viewSet(view, '_paperNotesLang', lang);
    }
  } catch (error: unknown) {
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
  const article = document.getElementById('paperReportContent')
    ?.querySelector('.paper-report-article');
  if (!article) return null;
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
  editor?.element.parentNode?.removeChild(editor.element);
  editor = null;
}

function refreshNotes(view: PaperNotesView): void {
  const article = document.getElementById('paperReportContent')
    ?.querySelector('.paper-report-article');
  if (article) decoratePaperNotes(article, view);
}

export function openPaperNoteEditor(
  anchor: NoteAnchor,
  existing: PaperNote | null,
  x: number,
  y: number,
): void {
  closePaperNoteEditor();
  const element = document.createElement('div');
  element.className = 'paper-note-editor';
  element.innerHTML = (anchor.quote
    ? `<div class="paper-note-editor-quote">${escape(anchor.quote.slice(0, 120))}</div>`
    : '')
    + `<textarea class="paper-note-editor-input" rows="3" placeholder="${escape(translate('paper.notePlaceholder'))}">${existing ? escape(existing.note) : ''}</textarea>`
    + '<div class="paper-note-editor-actions">'
    + `<button type="button" class="paper-note-save">${escape(translate('paper.noteSave'))}</button>`
    + (existing
      ? `<button type="button" class="paper-note-ask">${escape(translate('paper.noteAsk'))}</button><button type="button" class="paper-note-del">${escape(translate('paper.noteDelete'))}</button>`
      : '')
    + `<button type="button" class="paper-note-cancel">${escape(translate('paper.noteCancel'))}</button></div>`;
  document.body.appendChild(element);
  element.style.left = `${Math.max(8, Math.min(x - 120, window.innerWidth - 300))}px`;
  element.style.top = `${Math.max(8, y + 12)}px`;
  const current: NoteEditor = {
    element,
    noteId: existing?.id ?? null,
    anchor: existing?.anchor ?? anchor,
  };
  editor = current;
  const input = element.querySelector<HTMLTextAreaElement>('.paper-note-editor-input');
  input?.focus();
  element.querySelector('.paper-note-cancel')
    ?.addEventListener('click', closePaperNoteEditor);
  element.querySelector('.paper-note-save')?.addEventListener('click', async () => {
    const text = input?.value.trim() ?? '';
    if (!text) return;
    const view = notesView();
    try {
      if (current.noteId) {
        await api().notesUpdate(current.noteId, text);
        const notes = viewGet<PaperNote[]>(view, '_paperNotes') ?? [];
        notes.forEach((note) => {
          if (note.id === current.noteId) note.note = text;
        });
      } else {
        const data = await api().notesCreate({
          paper_hash: globals()._paperHash ?? '',
          lang: view.langKey?.() ?? '',
          anchor: current.anchor,
          note: text,
        });
        if (data.ok === true && data.note && typeof data.note === 'object') {
          const notes = viewGet<PaperNote[]>(view, '_paperNotes') ?? [];
          viewSet(view, '_paperNotes', notes.concat([data.note as PaperNote]));
        }
      }
    } catch (error: unknown) {
      console.warn('[Paper:Notes] save failed:', error);
    }
    closePaperNoteEditor();
    refreshNotes(view);
  });
  element.querySelector('.paper-note-ask')?.addEventListener('click', () => {
    const question = (current.anchor.quote ? `> ${current.anchor.quote}\n\n` : '')
      + (input?.value ?? '');
    closePaperNoteEditor();
    globals()._paperAskQuestion?.(question);
  });
  element.querySelector('.paper-note-del')?.addEventListener('click', async () => {
    const view = notesView();
    try {
      if (current.noteId) await api().notesDelete(current.noteId);
      const notes = viewGet<PaperNote[]>(view, '_paperNotes') ?? [];
      viewSet(view, '_paperNotes', notes.filter((note) => note.id !== current.noteId));
    } catch (error: unknown) {
      console.warn('[Paper:Notes] delete failed:', error);
    }
    closePaperNoteEditor();
    refreshNotes(view);
  });
}

export function noteFromSelection(): void {
  const anchor = noteAnchorFromSelection();
  if (!anchor) return;
  const selection = window.getSelection();
  const rect = selection?.rangeCount
    ? selection.getRangeAt(0).getBoundingClientRect()
    : { left: 200, bottom: 200 };
  openPaperNoteEditor(anchor, null, rect.left, rect.bottom);
  globals()._hidePaperQuoteBar?.();
}

export function notesAfterRender(
  article: Element,
  _container: Element | null,
  view: PaperNotesView,
): void {
  if (!view) return;
  const current = viewGet<PaperNote[]>(view, '_paperNotes');
  if (Array.isArray(current)) decoratePaperNotes(article, view);
  else void loadNotes(view).then(() => decoratePaperNotes(article, view));
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
    const view = notesView();
    const note = (viewGet<PaperNote[]>(view, '_paperNotes') ?? [])
      .find((candidate) => candidate.id === id);
    if (!note) return;
    const rect = chip.getBoundingClientRect();
    openPaperNoteEditor(note.anchor ?? {}, note, rect.left, rect.bottom);
  });
  scope.listen(document, 'keydown', (event) => {
    if ((event as KeyboardEvent).key === 'Escape' && editor) {
      closePaperNoteEditor();
    }
  });
  const destroy = (): void => {
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
state._paperNoteFromSelection = noteFromSelection;
state._paperNotesAfterRender = notesAfterRender;
state._paperNotesDecorate = decoratePaperNotes;
state._paperNoteOpenEditor = openPaperNoteEditor;
state._paperNoteAnchorFromSelection = noteAnchorFromSelection;
attachPaperNotes();
