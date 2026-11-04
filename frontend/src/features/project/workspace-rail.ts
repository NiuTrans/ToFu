/**
 * Shared project workspace rail: the staged-roots card and the searchable
 * recent-projects card mounted beside the shared folder browser
 * (./folder-browser) by both the Project Co-Pilot modal
 * (runtime/sections/project.js) and the sidebar folder settings dialog
 * (conversation/ui/folder-settings.ts), so the two surfaces share one markup,
 * one stylesheet set and one interaction model.
 *
 * Responsibility: rail DOM, the recent filter, drag/touch/keyboard reorder
 * gestures and focus after a host repaint. Hosts own the ordered root draft
 * (index 0 is the primary root) and every side effect through ports, and call
 * renderRoots() after each change they make to it.
 * Entry point: createWorkspaceRail. Dependencies: ../../ui, ./folder-icons,
 * ./presentation-assets. Styles: 10-project-{modal-shell,workspace,roots,
 * recent}.css; Tofu overrides in 12-tofu-theme-project-controls.css.
 */
import { createButton } from '../../ui';
import { folderIcon } from './folder-icons';
import type { FolderIconName } from './folder-icons';
import { PROJECT_PRESENTATION_ASSETS } from './presentation-assets';

export type WorkspaceRailTranslate = (key: string, values?: Record<string, string | number>) => string;

export interface RecentProject { path: string; count: number; exists: boolean }

export interface WorkspaceRailPorts {
  t: WorkspaceRailTranslate;
  /** Ordered staged roots; index 0 is the primary root. */
  roots: () => readonly string[];
  isReadOnly: (path: string) => boolean;
  /** Stage a typed path. Returns why it was refused, or null once handled. */
  add: (path: string) => string | null;
  remove: (index: number) => void;
  toggleReadOnly: (index: number) => void;
  /** Move the root at `from` so that it ends at index `to`. */
  move: (from: number, to: number) => void;
  /** A recent project that still exists was chosen. Returns why it could not be staged, if refused. */
  pickRecent: (path: string) => string | null | void;
  /** A recent path that no longer resolves was chosen; without it such rows are inert. */
  relinkRecent?: (path: string) => void;
  /** Offered only where the account's recent list may be cleared. */
  clearRecents?: () => void;
  /** Offered where a failed recent read may be requested again. */
  retryRecents?: () => void;
}

export interface WorkspaceRail {
  readonly el: HTMLElement;
  readonly input: HTMLInputElement;
  /** Repaint the staged roots after the host changed them. */
  renderRoots(): void;
  /** Publish the account's recent projects, or why they could not be read. */
  setRecents(result: readonly RecentProject[] | { error: string }): void;
  resetRecentFilter(): void;
  /** Stage a typed path that was not added yet; false when it was refused. */
  commitPendingPath(): boolean;
  /** Drop typed text and its refusal when the host replaces the whole draft. */
  clearPendingPath(): void;
  /** Announce a change to assistive technology. */
  announce(message: string): void;
  /** Re-read every label after a UI language switch. */
  relabel(): void;
}

type RowAsset = 'reorderGrip' | 'primaryStar' | 'workspaceFolder' | 'readOnlyLock' | 'writablePencil' | 'remove';
interface RowHit { row: HTMLElement; index: number; before: boolean }

let railSequence = 0;

function node<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}
/** PROJECT_PRESENTATION_ASSETS are trusted constants; nothing is interpolated. */
function asset(name: RowAsset): DocumentFragment {
  const template = document.createElement('template');
  template.innerHTML = PROJECT_PRESENTATION_ASSETS[name];
  return template.content;
}
function icon(name: FolderIconName, size: number): SVGSVGElement {
  const svg = folderIcon(name);
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  return svg;
}
function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}
function shortPath(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join('/')}` : path;
}
function appendHighlighted(parent: HTMLElement, text: string, query: string): void {
  const folded = text.toLowerCase();
  // Case folding that changes length would misplace the mark; show plain text.
  const at = query && folded.length === text.length ? folded.indexOf(query.toLowerCase()) : -1;
  if (at < 0) { parent.append(text); return; }
  parent.append(text.slice(0, at), node('mark', 'recent-hl', text.slice(at, at + query.length)), text.slice(at + query.length));
}

export function createWorkspaceRail(ports: WorkspaceRailPorts): WorkspaceRail {
  const { t } = ports;
  const seq = ++railSequence;
  const el = node('div', 'pm-rail');

  const workspace = node('section', 'pm-rail-card pm-rail-workspace');
  const workspaceLabel = node('div', 'pm-section-label');
  const workspaceTitle = node('span');
  workspaceTitle.id = `pm-rail-workspace-${seq}`;
  const workspaceCount = node('span', 'pm-count');
  workspaceLabel.append(workspaceTitle, workspaceCount);
  workspace.setAttribute('aria-labelledby', workspaceTitle.id);
  const addRow = node('div', 'mp-add-row');
  const input = node('input', 'mp-path-input');
  input.type = 'text';
  input.autocomplete = 'off';
  input.spellcheck = false;
  // Rail-owned: the legacy .btn rules only exist inside the Co-Pilot's .modal and
  // kit buttons take the project dialog's overrides, so neither renders alike in both hosts.
  const addButton = node('button', 'mp-add-btn');
  addButton.type = 'button';
  addButton.addEventListener('click', () => { submitTyped(true); });
  addRow.append(input, addButton);
  const addError = node('p', 'mp-add-error');
  addError.id = `pm-rail-add-error-${seq}`;
  addError.setAttribute('role', 'alert');
  addError.hidden = true;
  const rootList = node('ol', 'mp-folder-list');
  rootList.setAttribute('aria-labelledby', workspaceTitle.id);
  const status = node('p', 'pm-rail-status');
  status.setAttribute('role', 'status');
  workspace.append(workspaceLabel, addRow, addError, rootList, status);

  const recents = node('section', 'pm-rail-card recent-paths');
  recents.hidden = true;
  const recentLabel = node('div', 'pm-section-label');
  const recentTitle = node('span');
  recentTitle.id = `pm-rail-recent-${seq}`;
  const recentCount = node('span', 'pm-count');
  recentLabel.append(recentTitle, recentCount);
  recents.setAttribute('aria-labelledby', recentTitle.id);
  let clearAll: HTMLButtonElement | null = null;
  if (ports.clearRecents) {
    const clearRecents = ports.clearRecents;
    clearAll = node('button', 'recent-paths-clear');
    clearAll.type = 'button';
    clearAll.append(icon('trash', 13));
    clearAll.addEventListener('click', () => clearRecents());
    recentLabel.append(clearAll);
  }
  const search = node('div', 'recent-search');
  const filter = node('input', 'recent-search-input');
  filter.type = 'text';
  filter.autocomplete = 'off';
  filter.spellcheck = false;
  const clearFilter = node('button', 'recent-search-clear');
  clearFilter.type = 'button';
  clearFilter.hidden = true;
  clearFilter.append(icon('close', 11));
  search.append(filter, clearFilter);
  const recentStatus = node('p', 'recent-paths-empty');
  recentStatus.setAttribute('role', 'status');
  const recentError = node('p', 'recent-paths-error');
  recentError.setAttribute('role', 'alert');
  recentError.hidden = true;
  const retryRecents = ports.retryRecents;
  const retry = retryRecents ? createButton({ label: t('folder.settings.retry'), size: 'sm', onClick: () => retryRecents() }) : null;
  const retryRow = node('div', 'recent-paths-retry');
  retryRow.hidden = true;
  if (retry) retryRow.append(retry);
  const recentList = node('div', 'recent-paths-list');
  recents.append(recentLabel, search, recentStatus, recentError, retryRow, recentList);
  el.append(workspace, recents);

  let recentProjects: readonly RecentProject[] = [];
  let dragFrom: number | null = null;

  function paintLabels(): void {
    workspaceTitle.textContent = t('pm.workspace');
    input.placeholder = t('pm.pathPh');
    input.setAttribute('aria-label', t('pm.pathPh'));
    addButton.textContent = t('pm.add');
    recentTitle.textContent = t('pm.recent');
    filter.placeholder = t('pm.recentSearchPlaceholder');
    filter.setAttribute('aria-label', t('pm.recentSearchPlaceholder'));
    clearFilter.title = t('pm.recentClearSearch');
    clearFilter.setAttribute('aria-label', t('pm.recentClearSearch'));
    if (clearAll) {
      clearAll.title = t('pm.recentClearAll');
      clearAll.setAttribute('aria-label', t('pm.recentClearAll'));
    }
    if (retry) retry.textContent = t('folder.settings.retry');
  }

  function rowButton(className: string, glyph: RowAsset, title: string, label: string): HTMLButtonElement {
    const button = node('button', className);
    button.type = 'button';
    button.draggable = false;
    button.title = title;
    button.setAttribute('aria-label', label);
    button.append(asset(glyph));
    return button;
  }
  function focusRow(path: string, control: string): void {
    const row = [...rootList.children].find(child => (child as HTMLElement).dataset.path === path);
    row?.querySelector<HTMLElement>(control)?.focus();
  }
  function moveRoot(from: number, to: number, focusGrip: boolean): void {
    const path = ports.roots()[from];
    if (path === undefined || from === to) return;
    ports.move(from, to);
    const position = ports.roots().indexOf(path);
    if (position < 0) return;
    announce(t('folder.settings.reordered', { path, n: position + 1 }));
    if (focusGrip) focusRow(path, '.mp-row-grip');
  }
  function rootRow(path: string, index: number, total: number): HTMLLIElement {
    const primary = index === 0;
    const readOnly = ports.isReadOnly(path);
    const row = node('li', `mp-row${primary ? ' mp-row-primary' : ''}${readOnly ? ' mp-row-readonly' : ''}`);
    row.draggable = true;
    row.dataset.mpIdx = String(index);
    row.dataset.path = path;
    row.title = path;
    // The visible drag grip doubles as the keyboard reorder handle.
    const grip = node('button', 'mp-row-grip icon-box');
    grip.type = 'button';
    grip.title = t('pm.dragReorder');
    grip.setAttribute('aria-label', `${t('pm.dragReorder')}: ${path}`);
    grip.setAttribute('aria-keyshortcuts', 'ArrowUp ArrowDown');
    grip.append(asset('reorderGrip'));
    grip.addEventListener('keydown', event => {
      const step = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0;
      if (!step) return;
      event.preventDefault();
      const target = index + step;
      if (target >= 0 && target < total) moveRoot(index, target, true);
    });
    const glyph = node('span', 'mp-row-icon');
    glyph.append(asset(primary ? 'primaryStar' : 'workspaceFolder'));
    const text = node('span', 'mp-row-text');
    text.append(node('span', 'mp-row-name', basename(path)), node('span', 'mp-row-path', shortPath(path)));
    row.append(grip, glyph, text);
    if (primary) row.append(node('span', 'mp-row-badge', t('pm.rootBadge')));
    if (readOnly) row.append(node('span', 'mp-row-badge mp-row-badge-ro', t('pm.readOnlyBadge')));
    const lock = rowButton(`mp-row-lock${readOnly ? ' active' : ''}`, readOnly ? 'readOnlyLock' : 'writablePencil',
      t(readOnly ? 'pm.lockRo' : 'pm.lockRw'), `${t('pm.readOnlyBadge')}: ${path}`);
    lock.setAttribute('aria-pressed', String(readOnly));
    lock.addEventListener('click', () => { ports.toggleReadOnly(index); focusRow(path, '.mp-row-lock'); });
    const remove = rowButton('mp-row-remove', 'remove', t('common.remove'), `${t('common.remove')}: ${path}`);
    remove.addEventListener('click', () => { ports.remove(index); input.focus(); });
    row.append(lock, remove);
    return row;
  }
  function renderRoots(): void {
    const roots = ports.roots();
    workspaceCount.textContent = roots.length ? String(roots.length) : '';
    rootList.replaceChildren(...roots.map((path, index) => rootRow(path, index, roots.length)));
    if (!roots.length) rootList.append(node('li', 'mp-empty-hint', t('pm.emptyFolders')));
  }

  /** `typed` marks the field invalid; a refused recent pick only explains itself. */
  function showAddError(message: string | null, typed = true): void {
    addError.textContent = message ?? '';
    addError.hidden = !message;
    if (message && typed) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
    if (message) input.setAttribute('aria-describedby', addError.id);
    else input.removeAttribute('aria-describedby');
  }
  function submitTyped(focusAfter: boolean): boolean {
    const refusal = ports.add(input.value.trim());
    showAddError(refusal);
    if (refusal) { input.focus(); return false; }
    input.value = '';
    if (focusAfter) input.focus();
    return true;
  }
  input.addEventListener('input', () => showAddError(null));
  input.addEventListener('keydown', event => {
    // An IME commit also fires Enter; it confirms text, not the Add action.
    if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      submitTyped(true);
    }
  });

  function rowAt(target: EventTarget | null, clientY: number): RowHit | null {
    const row = target instanceof Element ? target.closest<HTMLElement>('.mp-row[data-mp-idx]') : null;
    if (!row || !rootList.contains(row)) return null;
    const index = Number(row.dataset.mpIdx);
    if (!Number.isInteger(index)) return null;
    const rect = row.getBoundingClientRect();
    return { row, index, before: rect.height ? clientY - rect.top < rect.height / 2 : true };
  }
  function clearDropMarks(): void {
    rootList.querySelectorAll('.mp-drop-before, .mp-drop-after').forEach(row => row.classList.remove('mp-drop-before', 'mp-drop-after'));
  }
  function markDrop(hit: RowHit | null): void {
    clearDropMarks();
    hit?.row.classList.add(hit.before ? 'mp-drop-before' : 'mp-drop-after');
  }
  function endDrag(): void {
    clearDropMarks();
    rootList.querySelectorAll('.mp-row-dragging').forEach(row => row.classList.remove('mp-row-dragging'));
    dragFrom = null;
  }
  function drop(hit: RowHit | null): void {
    const from = dragFrom;
    endDrag();
    if (from === null || !hit) return;
    // Removing `from` first shifts every later row up by one.
    const to = hit.before ? hit.index : hit.index + 1;
    moveRoot(from, from < to ? to - 1 : to, false);
  }
  rootList.addEventListener('dragstart', event => {
    const target = event.target instanceof Element ? event.target : null;
    // Row controls keep their click semantics; the grip is the drag handle.
    if (target?.closest('button:not(.mp-row-grip)')) { event.preventDefault(); return; }
    const hit = rowAt(target, event.clientY);
    if (!hit) return;
    dragFrom = hit.index;
    hit.row.classList.add('mp-row-dragging');
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      // Firefox starts a drag only when it carries a payload.
      event.dataTransfer.setData('text/plain', String(hit.index));
    }
  });
  rootList.addEventListener('dragover', event => {
    if (dragFrom === null) return; // A file drag belongs to the browser pane.
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    markDrop(rowAt(event.target, event.clientY));
  });
  rootList.addEventListener('dragleave', event => {
    if (dragFrom !== null && event.target === rootList && !rootList.contains(event.relatedTarget as Node | null)) clearDropMarks();
  });
  rootList.addEventListener('drop', event => {
    if (dragFrom === null) return;
    // A page-level file-drop handler must never receive the row payload.
    event.preventDefault();
    event.stopPropagation();
    drop(rowAt(event.target, event.clientY));
  });
  rootList.addEventListener('dragend', endDrag);
  // Touch has no HTML5 drag events. Start from the grip only so a finger
  // elsewhere still scrolls the list; touchmove holds the page still.
  rootList.addEventListener('touchstart', event => {
    const touch = event.touches[0];
    const target = event.target instanceof Element ? event.target : null;
    if (!touch || !target?.closest('.mp-row-grip')) return;
    const hit = rowAt(target, touch.clientY);
    if (!hit) return;
    dragFrom = hit.index;
    hit.row.classList.add('mp-row-dragging');
  }, { passive: true });
  rootList.addEventListener('touchmove', event => {
    const touch = event.touches[0];
    if (dragFrom === null || !touch) return;
    event.preventDefault();
    markDrop(rowAt(document.elementFromPoint(touch.clientX, touch.clientY), touch.clientY));
  }, { passive: false });
  rootList.addEventListener('touchend', event => {
    if (dragFrom === null) return;
    const touch = event.changedTouches[0];
    drop(touch ? rowAt(document.elementFromPoint(touch.clientX, touch.clientY), touch.clientY) : null);
  });
  rootList.addEventListener('touchcancel', endDrag);

  function recentRow(item: RecentProject, query: string): HTMLButtonElement {
    const missing = !item.exists;
    const relink = missing ? ports.relinkRecent : undefined;
    const entry = node('button', `recent-path-item${missing ? ' recent-path-missing' : ''}`);
    entry.type = 'button';
    entry.dataset.path = item.path;
    entry.title = missing
      ? (relink ? t('pm.recentMissingTitle', { path: item.path }) : `${item.path} — ${t('pm.recentMissing')}`)
      : item.path;
    entry.setAttribute('aria-label', missing ? `${item.path} — ${t('pm.recentMissing')}` : item.path);
    entry.disabled = missing && !relink;
    const name = node('span', 'recent-path-name');
    appendHighlighted(name, basename(item.path), query);
    if (missing) name.append(node('span', 'recent-missing-badge', t('pm.recentMissing')));
    // RTL box + isolated LTR path: overflow trims the start, keeping the tail.
    const full = node('span', 'recent-path-full');
    const isolate = node('bdi');
    isolate.dir = 'ltr';
    appendHighlighted(isolate, item.path, query);
    full.append(isolate);
    const text = node('span', 'recent-path-text');
    text.append(name, full);
    entry.append(text);
    if (!missing && item.count > 1) entry.append(node('span', 'recent-path-count', `×${item.count}`));
    entry.addEventListener('click', () => {
      if (missing) { relink?.(item.path); return; }
      const refusal = ports.pickRecent(item.path);
      if (refusal) showAddError(refusal, false);
    });
    return entry;
  }
  function renderRecents(): void {
    const query = filter.value.trim();
    const needle = query.toLowerCase();
    const visible = needle ? recentProjects.filter(item => item.path.toLowerCase().includes(needle)) : recentProjects;
    recents.hidden = !recentProjects.length && recentError.hidden;
    recentCount.textContent = !recentProjects.length ? '' : needle ? `${visible.length}/${recentProjects.length}` : String(recentProjects.length);
    clearFilter.hidden = !query;
    recentStatus.textContent = visible.length || !recentProjects.length ? '' : t('pm.recentNoMatch');
    recentList.replaceChildren(...visible.map(item => recentRow(item, query)));
  }
  function setRecents(result: readonly RecentProject[] | { error: string }): void {
    const failure = Array.isArray(result) ? null : (result as { error: string }).error;
    if (failure === null) recentProjects = result as readonly RecentProject[];
    recentError.textContent = failure ?? '';
    recentError.hidden = failure === null;
    retryRow.hidden = failure === null || !retry;
    renderRecents();
  }
  function clearQuery(): void {
    filter.value = '';
    renderRecents();
  }
  filter.addEventListener('input', renderRecents);
  filter.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      recentList.querySelector<HTMLButtonElement>('.recent-path-item:not(:disabled)')?.click();
    } else if (event.key === 'Escape' && filter.value) {
      // The first Escape clears the query; only a second one reaches the dialog.
      event.preventDefault();
      event.stopPropagation();
      clearQuery();
    }
  });
  clearFilter.addEventListener('click', () => { clearQuery(); filter.focus(); });

  function announce(message: string): void {
    status.textContent = message;
  }

  paintLabels();
  renderRoots();
  renderRecents();
  return {
    el,
    input,
    renderRoots,
    setRecents,
    resetRecentFilter: clearQuery,
    commitPendingPath: () => !input.value.trim() || submitTyped(false),
    clearPendingPath: () => { input.value = ''; showAddError(null); },
    announce,
    relabel: () => { paintLabels(); renderRoots(); renderRecents(); },
  };
}
