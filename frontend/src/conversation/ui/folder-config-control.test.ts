// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { installFolderConfigInteractions, makeFolderContextMenuAccessible, mountFolderConfigControls } from './folder-config-control';
afterEach(() => document.body.replaceChildren());
function rail() {
  const el = document.createElement('div');
  el.innerHTML = '<button class="folder-tab" data-folder-id="">Inbox</button><div class="folder-tab-row"><button class="folder-tab" data-folder-id="project" title="Project"><span class="folder-tab-dot">P</span><span class="folder-tab-name">Project</span></button></div><button class="folder-tab folder-tab-add"><span class="folder-tab-dot folder-tab-add-dot">+</span><span class="folder-tab-name">New project</span></button>';
  document.body.append(el); return el;
}
describe('folder rail settings', () => {
  it('preserves add tile node/classes/glyph across mounts so delegated clicks work', () => {
    const el = rail(); const add = el.querySelector<HTMLButtonElement>('.folder-tab-add')!;
    const glyph = add.firstElementChild; const create = vi.fn();
    el.addEventListener('click', event => {
      const tab = (event.target as Element).closest('.folder-tab');
      if (tab?.classList.contains('folder-tab-add')) create();
    });
    mountFolderConfigControls(el, 'Project settings'); mountFolderConfigControls(el, 'Project settings');
    expect(el.querySelector('.folder-tab-add')).toBe(add); expect(add.firstElementChild).toBe(glyph);
    expect(add.getAttribute('aria-haspopup')).toBe('dialog');
    glyph!.dispatchEvent(new MouseEvent('click', { bubbles: true })); expect(create).toHaveBeenCalledOnce();
    expect(el.querySelectorAll('.folder-tab-settings')).toHaveLength(1);
    expect(el.querySelector('[data-folder-id="project"]')?.getAttribute('title')).toBe('Project\nProject settings');
  });
  it('opens the captured folder on double click; ignores inbox/add and supports teardown', () => {
    const el = rail(); const settings = vi.fn(); const context = vi.fn();
    const dispose = installFolderConfigInteractions(el, { settings, context });
    el.querySelector('.folder-tab-dot')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    expect(settings).toHaveBeenCalledExactlyOnceWith('project');
    el.querySelector('.folder-tab-add')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    el.querySelector('[data-folder-id=""]')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    expect(settings).toHaveBeenCalledOnce(); dispose();
    el.querySelector('[data-folder-id="project"]')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    expect(settings).toHaveBeenCalledOnce();
  });
  it('supports Shift+F10/ContextMenu without stealing selection keys and disposes keyboard listeners', () => {
    const el = rail(); const context = vi.fn();
    const dispose = installFolderConfigInteractions(el, { settings: vi.fn(), context });
    const tab = el.querySelector('[data-folder-id="project"]')!;
    tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true, cancelable: true }));
    tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ContextMenu', bubbles: true }));
    tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect(context.mock.calls).toEqual([['project', 0, 0], ['project', 0, 0]]); dispose();
    tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ContextMenu', bubbles: true }));
    expect(context).toHaveBeenCalledTimes(2);
  });
  it('provides focus, arrow navigation, activation and Escape restoration for context actions', () => {
    const el = rail(); const tab = el.querySelector<HTMLButtonElement>('[data-folder-id="project"]')!; tab.focus();
    const menu = document.createElement('div'); menu.innerHTML = '<div class="folder-tab-menu-item">Settings</div><div class="folder-tab-menu-item">Delete</div>';
    document.body.append(menu); const activate = vi.fn(); menu.lastElementChild!.addEventListener('click', activate);
    makeFolderContextMenuAccessible(menu); expect(document.activeElement).toBe(menu.firstElementChild);
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    expect(document.activeElement).toBe(menu.lastElementChild);
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); expect(activate).toHaveBeenCalledOnce();
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(menu.isConnected).toBe(false); expect(document.activeElement).toBe(tab);
  });
});
