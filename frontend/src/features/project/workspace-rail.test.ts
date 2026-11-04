// @vitest-environment jsdom
/** Behavior specification for the shared project workspace rail mounted by the
 * Project Co-Pilot modal and sidebar folder settings. Real rail; the fake host
 * owns an ordered root draft and repaints after each change, as both hosts do. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createWorkspaceRail } from './workspace-rail';
import type { WorkspaceRail, WorkspaceRailPorts } from './workspace-rail';

const t = (key: string, values?: Record<string, string | number>) => `${key}${values ? JSON.stringify(values) : ''}`;

function host(initial: string[] = [], overrides: Partial<WorkspaceRailPorts> = {}) {
  const roots = [...initial];
  const readOnly = new Set<string>();
  let rail: WorkspaceRail | null = null;
  const repaint = () => rail?.renderRoots();
  const spies = {
    add: vi.fn((path: string) => {
      if (!path) return 'required';
      if (roots.includes(path)) return 'duplicate';
      roots.push(path); repaint();
      return null;
    }),
    remove: vi.fn((index: number) => {
      const [path] = roots.splice(index, 1);
      if (path !== undefined) readOnly.delete(path);
      repaint();
    }),
    toggleReadOnly: vi.fn((index: number) => {
      const path = roots[index];
      if (readOnly.has(path)) readOnly.delete(path); else readOnly.add(path);
      repaint();
    }),
    move: vi.fn((from: number, to: number) => {
      const [path] = roots.splice(from, 1);
      roots.splice(to, 0, path);
      repaint();
    }),
    pickRecent: vi.fn((_path: string): string | null => null),
  };
  rail = createWorkspaceRail({ t, roots: () => roots, isReadOnly: path => readOnly.has(path), ...spies, ...overrides });
  document.body.append(rail.el);
  return { rail, roots, readOnly, ...spies };
}
afterEach(() => { document.body.replaceChildren(); });

const rows = (rail: WorkspaceRail) => [...rail.el.querySelectorAll<HTMLLIElement>('ol.mp-folder-list > li.mp-row')];
const rowPaths = (rail: WorkspaceRail) => rows(rail).map(row => row.dataset.path);
const control = (row: Element, selector: string) => row.querySelector<HTMLButtonElement>(selector)!;
const recentRows = (rail: WorkspaceRail) => [...rail.el.querySelectorAll<HTMLButtonElement>('.recent-paths-list > button.recent-path-item')];
const recentCard = (rail: WorkspaceRail) => rail.el.querySelector<HTMLElement>('section.pm-rail-card.recent-paths')!;
const filter = (rail: WorkspaceRail) => rail.el.querySelector<HTMLInputElement>('.recent-search > input.recent-search-input')!;
function type(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}
function key(target: Element, name: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}
/** jsdom has neither DragEvent nor touch layout; carry the fields the rail reads. */
function gesture(type: string, target: Element, fields: Record<string, unknown> = {}): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, { clientY: 0, dataTransfer: { effectAllowed: '', dropEffect: '', setData: vi.fn() }, ...fields });
  target.dispatchEvent(event);
  return event;
}
/** Rows 20px tall, stacked from y=0, so pointer halves are deterministic. */
function layout(rail: WorkspaceRail): void {
  rows(rail).forEach((row, index) => {
    row.getBoundingClientRect = () => ({ top: index * 20, height: 20 }) as DOMRect;
  });
}

describe('workspace rail roots', () => {
  it('renders the Co-Pilot rows: primary first, short path, badges and counts', () => {
    const { rail, readOnly } = host(['/srv/a/b/alpha', '/srv/beta']);
    readOnly.add('/srv/beta');
    rail.renderRoots();
    expect(rail.el.matches('div.pm-rail')).toBe(true);
    expect(rail.el.querySelector('.pm-rail-workspace .pm-count')!.textContent).toBe('2');
    const [first, second] = rows(rail);
    expect(first.classList.contains('mp-row-primary')).toBe(true);
    expect(first.title).toBe('/srv/a/b/alpha');
    expect(first.querySelector('.mp-row-name')!.textContent).toBe('alpha');
    expect(first.querySelector('.mp-row-path')!.textContent).toBe('…/b/alpha');
    expect(first.querySelector('.mp-row-badge')!.textContent).toBe('pm.rootBadge');
    expect(control(first, '.mp-row-lock').getAttribute('aria-pressed')).toBe('false');
    expect(second.classList.contains('mp-row-primary')).toBe(false);
    expect(second.classList.contains('mp-row-readonly')).toBe(true);
    expect(second.querySelector('.mp-row-badge-ro')!.textContent).toBe('pm.readOnlyBadge');
    expect(control(second, '.mp-row-lock').getAttribute('aria-pressed')).toBe('true');
    expect(control(second, '.mp-row-remove').getAttribute('aria-label')).toBe('common.remove: /srv/beta');
    expect(rows(rail).every(row => row.draggable)).toBe(true);
    expect([...rail.el.querySelectorAll<HTMLButtonElement>('.mp-row-lock, .mp-row-remove')].every(button => !button.draggable)).toBe(true);

    const empty = host().rail;
    expect(empty.el.querySelector('li.mp-empty-hint')!.textContent).toBe('pm.emptyFolders');
    expect(empty.el.querySelector('.pm-rail-workspace .pm-count')!.textContent).toBe('');
  });

  it('routes lock and remove through the host and keeps keyboard focus in the rail', () => {
    const { rail, toggleReadOnly, remove } = host(['/a', '/b']);
    const lock = control(rows(rail)[1], '.mp-row-lock');
    lock.focus();
    lock.click();
    expect(toggleReadOnly).toHaveBeenCalledWith(1);
    const repainted = control(rows(rail)[1], '.mp-row-lock');
    expect(repainted.getAttribute('aria-pressed')).toBe('true');
    expect(document.activeElement).toBe(repainted);

    control(rows(rail)[0], '.mp-row-remove').click();
    expect(remove).toHaveBeenCalledWith(0);
    expect(rowPaths(rail)).toEqual(['/b']);
    expect(rows(rail)[0].classList.contains('mp-row-primary')).toBe(true);
    expect(document.activeElement).toBe(rail.input);
  });

  it('reorders from the keyboard on the grip and announces the new position', () => {
    const { rail, move } = host(['/a', '/b', '/c']);
    const grip = control(rows(rail)[2], '.mp-row-grip');
    expect(grip.getAttribute('aria-label')).toBe('pm.dragReorder: /c');
    key(grip, 'ArrowUp');
    expect(move).toHaveBeenLastCalledWith(2, 1);
    expect(rowPaths(rail)).toEqual(['/a', '/c', '/b']);
    expect(document.activeElement).toBe(control(rows(rail)[1], '.mp-row-grip'));

    key(document.activeElement!, 'ArrowUp');
    expect(rowPaths(rail)).toEqual(['/c', '/a', '/b']);
    expect(rows(rail)[0].classList.contains('mp-row-primary')).toBe(true);
    expect(rail.el.querySelector('.pm-rail-status')!.textContent).toBe('folder.settings.reordered{"path":"/c","n":1}');

    key(control(rows(rail)[0], '.mp-row-grip'), 'ArrowUp');
    key(control(rows(rail)[2], '.mp-row-grip'), 'ArrowDown');
    expect(move).toHaveBeenCalledTimes(2);
  });

  it('drags rows into place and leaves control and file drags alone', () => {
    const { rail, move } = host(['/a', '/b', '/c']);
    layout(rail);
    const [a, , c] = rows(rail);
    expect(gesture('dragstart', c, { clientY: 50 }).defaultPrevented).toBe(false);
    expect(c.classList.contains('mp-row-dragging')).toBe(true);
    expect(gesture('dragover', a, { clientY: 5 }).defaultPrevented).toBe(true);
    expect(a.classList.contains('mp-drop-before')).toBe(true);
    gesture('drop', a, { clientY: 5 });
    expect(move).toHaveBeenLastCalledWith(2, 0);
    expect(rowPaths(rail)).toEqual(['/c', '/a', '/b']);
    expect(rail.el.querySelector('.mp-drop-before, .mp-drop-after, .mp-row-dragging')).toBeNull();

    layout(rail);
    const [top, , bottom] = rows(rail);
    gesture('dragstart', control(top, '.mp-row-grip'), { clientY: 5 });
    gesture('dragover', bottom, { clientY: 55 });
    expect(bottom.classList.contains('mp-drop-after')).toBe(true);
    gesture('drop', bottom, { clientY: 55 });
    expect(move).toHaveBeenLastCalledWith(0, 2);
    expect(rowPaths(rail)).toEqual(['/a', '/b', '/c']);

    layout(rail);
    const middle = rows(rail)[1];
    gesture('dragstart', middle, { clientY: 25 });
    gesture('drop', middle, { clientY: 25 });
    expect(move).toHaveBeenCalledTimes(2);

    const lockDrag = gesture('dragstart', control(rows(rail)[0], '.mp-row-lock'));
    expect(lockDrag.defaultPrevented).toBe(true);
    expect(rail.el.querySelector('.mp-row-dragging')).toBeNull();
    // Without a row drag in flight, a dragged file belongs to the browser pane.
    expect(gesture('dragover', rows(rail)[0]).defaultPrevented).toBe(false);
  });

  it('starts touch drags from the grip only', () => {
    const { rail, move } = host(['/a', '/b']);
    layout(rail);
    const [a, b] = rows(rail);
    gesture('touchstart', a.querySelector('.mp-row-name')!, { touches: [{ clientX: 0, clientY: 5 }] });
    expect(a.classList.contains('mp-row-dragging')).toBe(false);

    gesture('touchstart', control(b, '.mp-row-grip'), { touches: [{ clientX: 0, clientY: 25 }] });
    expect(b.classList.contains('mp-row-dragging')).toBe(true);
    const elementFromPoint = document.elementFromPoint;
    document.elementFromPoint = () => a;
    try {
      expect(gesture('touchmove', b, { touches: [{ clientX: 0, clientY: 5 }] }).defaultPrevented).toBe(true);
      expect(a.classList.contains('mp-drop-before')).toBe(true);
      gesture('touchend', b, { changedTouches: [{ clientX: 0, clientY: 5 }] });
    } finally {
      document.elementFromPoint = elementFromPoint;
    }
    expect(move).toHaveBeenCalledWith(1, 0);
    expect(rowPaths(rail)).toEqual(['/b', '/a']);
  });

  it('stages typed paths through the host and keeps refusals explainable', () => {
    const { rail, add } = host(['/a']);
    const addButton = rail.el.querySelector<HTMLButtonElement>('.mp-add-row > button.mp-add-btn')!;
    expect(addButton.textContent).toBe('pm.add');
    expect(rail.input.getAttribute('aria-label')).toBe('pm.pathPh');
    type(rail.input, '  /b  ');
    key(rail.input, 'Enter');
    expect(add).toHaveBeenLastCalledWith('/b');
    expect(rail.input.value).toBe('');
    expect(rowPaths(rail)).toEqual(['/a', '/b']);

    // An IME commit confirms text; it is not the Add action.
    type(rail.input, '/c');
    key(rail.input, 'Enter', { isComposing: true });
    expect(add).toHaveBeenCalledTimes(1);

    type(rail.input, '/a');
    addButton.click();
    const error = rail.el.querySelector<HTMLElement>('.mp-add-error')!;
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe('duplicate');
    expect(rail.input.value).toBe('/a');
    expect(rail.input.getAttribute('aria-invalid')).toBe('true');
    expect(rail.input.getAttribute('aria-describedby')).toBe(error.id);
    type(rail.input, '/a2');
    expect(error.hidden).toBe(true);
    expect(rail.input.hasAttribute('aria-invalid')).toBe(false);

    // Hosts commit a pending path before saving; a refusal blocks the save.
    expect(rail.commitPendingPath()).toBe(true);
    expect(rowPaths(rail)).toEqual(['/a', '/b', '/a2']);
    type(rail.input, '/b');
    expect(rail.commitPendingPath()).toBe(false);
    expect(rail.input.getAttribute('aria-invalid')).toBe('true');
    rail.clearPendingPath();
    expect(rail.input.value).toBe('');
    expect(error.hidden).toBe(true);
    expect(rail.commitPendingPath()).toBe(true);
    expect(add).toHaveBeenCalledTimes(4);
  });
});

describe('workspace rail recents', () => {
  it('renders names, isolated full paths, counts and missing state; hides while empty', () => {
    const { rail } = host();
    expect(recentCard(rail).hidden).toBe(true);
    rail.setRecents([
      { path: '/work/alpha', count: 3, exists: true },
      { path: '/work/beta', count: 1, exists: true },
      { path: '/gone/gamma', count: 2, exists: false },
    ]);
    expect(recentCard(rail).hidden).toBe(false);
    expect(recentCard(rail).querySelector('.pm-count')!.textContent).toBe('3');
    const [alpha, beta, gamma] = recentRows(rail);
    expect(alpha.querySelector('.recent-path-name')!.textContent).toBe('alpha');
    const isolate = alpha.querySelector('.recent-path-full > bdi')!;
    expect(isolate.getAttribute('dir')).toBe('ltr');
    expect(isolate.textContent).toBe('/work/alpha');
    expect(alpha.getAttribute('aria-label')).toBe('/work/alpha');
    expect(alpha.querySelector('.recent-path-count')!.textContent).toBe('×3');
    expect(beta.querySelector('.recent-path-count')).toBeNull();
    expect(gamma.classList.contains('recent-path-missing')).toBe(true);
    expect(gamma.querySelector('.recent-missing-badge')!.textContent).toBe('pm.recentMissing');
    expect(gamma.querySelector('.recent-path-count')).toBeNull();
    expect(gamma.disabled).toBe(true);
    rail.setRecents([]);
    expect(recentCard(rail).hidden).toBe(true);
  });

  it('routes picks through the host and never disables a staged recent', () => {
    const relinkRecent = vi.fn();
    const { rail, pickRecent } = host(['/work/alpha'], { relinkRecent });
    rail.setRecents([{ path: '/work/alpha', count: 1, exists: true }, { path: '/gone', count: 1, exists: false }]);
    const [alpha, gone] = recentRows(rail);
    expect(alpha.disabled).toBe(false);
    alpha.click();
    expect(pickRecent).toHaveBeenCalledWith('/work/alpha');
    expect(gone.disabled).toBe(false);
    expect(gone.title).toBe('pm.recentMissingTitle{"path":"/gone"}');
    gone.click();
    expect(relinkRecent).toHaveBeenCalledWith('/gone');
    expect(pickRecent).toHaveBeenCalledTimes(1);
  });

  it('explains a refused pick without marking the typed field invalid', () => {
    const { rail } = host([], { pickRecent: () => 'limit' });
    rail.setRecents([{ path: '/x', count: 1, exists: true }]);
    recentRows(rail)[0].click();
    const error = rail.el.querySelector<HTMLElement>('.mp-add-error')!;
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe('limit');
    expect(rail.input.hasAttribute('aria-invalid')).toBe(false);
  });

  it('filters with counts and highlights; Enter picks, Escape clears before the dialog sees it', () => {
    const { rail, pickRecent } = host();
    rail.setRecents([
      { path: '/work/Alpha', count: 1, exists: true },
      { path: '/work/beta', count: 1, exists: true },
      { path: '/old/alphabet', count: 1, exists: false },
    ]);
    const clear = rail.el.querySelector<HTMLButtonElement>('.recent-search-clear')!;
    expect(clear.hidden).toBe(true);
    type(filter(rail), 'ALPHA');
    expect(recentRows(rail).map(row => row.dataset.path)).toEqual(['/work/Alpha', '/old/alphabet']);
    expect(recentCard(rail).querySelector('.pm-count')!.textContent).toBe('2/3');
    expect(clear.hidden).toBe(false);
    expect([...recentRows(rail)[0].querySelectorAll('mark.recent-hl')].map(mark => mark.textContent)).toEqual(['Alpha', 'Alpha']);
    key(filter(rail), 'Enter');
    expect(pickRecent).toHaveBeenCalledWith('/work/Alpha');

    type(filter(rail), 'nothing');
    expect(recentRows(rail)).toHaveLength(0);
    expect(rail.el.querySelector('.recent-paths-empty')!.textContent).toBe('pm.recentNoMatch');
    const outside = vi.fn();
    document.body.addEventListener('keydown', outside);
    key(filter(rail), 'Escape');
    expect(filter(rail).value).toBe('');
    expect(outside).not.toHaveBeenCalled();
    expect(recentRows(rail)).toHaveLength(3);
    expect(rail.el.querySelector('.recent-paths-empty')!.textContent).toBe('');
    key(filter(rail), 'Escape');
    expect(outside).toHaveBeenCalledTimes(1);
    document.body.removeEventListener('keydown', outside);

    type(filter(rail), 'beta');
    clear.click();
    expect(filter(rail).value).toBe('');
    expect(document.activeElement).toBe(filter(rail));
    type(filter(rail), 'beta');
    rail.resetRecentFilter();
    expect(recentRows(rail)).toHaveLength(3);
  });

  it('renders crafted paths as text and hands them to the host verbatim', () => {
    const crafted = [
      '/tmp/a"),alert(1),("',
      '/tmp/<img src=x onerror=alert(1)>',
      `/tmp/it's "quoted" & <b>bold</b>`,
      '/tmp/\\"); selectRecentProject("/etc',
    ];
    const relinkRecent = vi.fn();
    const { rail, pickRecent } = host([], { relinkRecent });
    rail.setRecents(crafted.map((path, index) => ({ path, count: 2, exists: index % 2 === 0 })));
    expect(rail.el.querySelector('img, b')).toBeNull();
    expect(recentRows(rail).map(row => row.querySelector('.recent-path-full')!.textContent)).toEqual(crafted);
    type(filter(rail), '<img');
    expect(rail.el.querySelector('img')).toBeNull();
    expect(recentRows(rail)[0].querySelector('mark.recent-hl')!.textContent).toBe('<img');
    rail.resetRecentFilter();
    recentRows(rail).forEach(row => row.click());
    expect(pickRecent.mock.calls.map(([path]) => path)).toEqual([crafted[0], crafted[2]]);
    expect(relinkRecent.mock.calls.map(([path]) => path)).toEqual([crafted[1], crafted[3]]);
  });

  it('keeps a failed read visible with a retry and offers clear-all only where supported', () => {
    const retryRecents = vi.fn();
    const { rail } = host([], { retryRecents });
    expect(rail.el.querySelector('.recent-paths-clear')).toBeNull();
    rail.setRecents({ error: 'offline' });
    expect(recentCard(rail).hidden).toBe(false);
    const error = rail.el.querySelector<HTMLElement>('.recent-paths-error')!;
    const retryRow = rail.el.querySelector<HTMLElement>('.recent-paths-retry')!;
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe('offline');
    expect(retryRow.hidden).toBe(false);
    retryRow.querySelector('button')!.click();
    expect(retryRecents).toHaveBeenCalledTimes(1);
    rail.setRecents([{ path: '/x', count: 1, exists: true }]);
    expect(error.hidden).toBe(true);
    expect(retryRow.hidden).toBe(true);

    const clearRecents = vi.fn();
    const offered = host([], { clearRecents }).rail;
    const clearAll = offered.el.querySelector<HTMLButtonElement>('.pm-section-label > button.recent-paths-clear')!;
    expect(clearAll.getAttribute('aria-label')).toBe('pm.recentClearAll');
    clearAll.click();
    expect(clearRecents).toHaveBeenCalledTimes(1);
  });

  it('re-reads every label after a language switch', () => {
    let language = 'en';
    const { rail } = host(['/a'], { t: (name: string) => `${language}:${name}` });
    rail.setRecents([{ path: '/r', count: 1, exists: true }]);
    const title = rail.el.querySelector('.pm-rail-workspace .pm-section-label > span')!;
    expect(title.textContent).toBe('en:pm.workspace');
    language = 'zh';
    rail.relabel();
    expect(title.textContent).toBe('zh:pm.workspace');
    expect(rail.input.placeholder).toBe('zh:pm.pathPh');
    expect(filter(rail).placeholder).toBe('zh:pm.recentSearchPlaceholder');
    expect(recentCard(rail).querySelector('.pm-section-label > span')!.textContent).toBe('zh:pm.recent');
    expect(rows(rail)[0].querySelector('.mp-row-badge')!.textContent).toBe('zh:pm.rootBadge');
  });
});
