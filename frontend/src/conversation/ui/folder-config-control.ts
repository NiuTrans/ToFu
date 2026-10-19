/** Folder rail configuration affordance. Retained rendering calls this bridge
 * until the rail moves to TypeScript; selection and dialog state stay with
 * their existing owners. Never replace selection/add tiles: delegated rail
 * interactions and shared tile geometry depend on their markup. */
import { createIconButton } from '../../ui';

export function mountFolderConfigControls(rail: HTMLElement, label: string): void {
  rail.querySelectorAll<HTMLElement>('.folder-tab-row').forEach(row => {
    const selection = row.querySelector<HTMLButtonElement>('.folder-tab');
    if (!selection) return;
    const icon = rail.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('width', '16');
    icon.setAttribute('height', '16');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('fill', 'currentColor');
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/>';
    row.querySelector('.folder-tab-settings')?.remove();
    const name = selection.querySelector('.folder-tab-name')?.textContent ?? selection.title;
    const button = createIconButton({ icon, label: `${label}: ${name}`, size: 'sm' });
    button.setAttribute('aria-haspopup', 'dialog');
    button.title = `${label}: ${name}`;
    button.classList.add('folder-tab-settings');
    button.dataset.folderConfigId = selection.dataset.folderId;
    selection.setAttribute('aria-keyshortcuts', 'Shift+F10');
    if (!selection.title.endsWith(`\n${label}`)) selection.title += `\n${label}`;
    row.appendChild(button);
  });

  const add = rail.querySelector<HTMLButtonElement>('.folder-tab-add');
  if (add) {
    add.classList.add('folder-tab');
    add.type = 'button';
    add.setAttribute('aria-label', add.textContent?.trim() ?? add.title);
    add.setAttribute('aria-haspopup', 'dialog');
  }
}

/** Delegated listeners survive active-tab paints; page composition owns the
 * returned disposal function. No delayed single-click or second selection state. */
export function installFolderConfigInteractions(rail: HTMLElement, ports: {
  settings: (id: string) => void;
  context: (id: string, x: number, y: number) => void;
}): () => void {
  const folderTab = (target: EventTarget | null) => {
    const ElementType = rail.ownerDocument.defaultView?.Element;
    const tab = ElementType && target instanceof ElementType ? target.closest<HTMLElement>('.folder-tab[data-folder-id]') : null;
    return tab && rail.contains(tab) && tab.dataset.folderId ? tab : null;
  };
  const doubleClick = (event: MouseEvent) => {
    const tab = folderTab(event.target);
    if (!tab) return;
    event.preventDefault(); event.stopPropagation();
    ports.settings(tab.dataset.folderId!);
  };
  const keydown = (event: KeyboardEvent) => {
    if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
    const tab = folderTab(event.target);
    if (!tab) return;
    event.preventDefault(); event.stopPropagation();
    const rect = tab.getBoundingClientRect();
    ports.context(tab.dataset.folderId!, rect.left, rect.bottom);
  };
  rail.addEventListener('dblclick', doubleClick);
  rail.addEventListener('keydown', keydown);
  return () => {
    rail.removeEventListener('dblclick', doubleClick);
    rail.removeEventListener('keydown', keydown);
  };
}

/** Adapt the retained context menu to native keyboard-operable actions. */
export function makeFolderContextMenuAccessible(menu: HTMLElement): void {
  menu.setAttribute('role', 'menu');
  const previousFocus = menu.ownerDocument.activeElement as HTMLElement | null;
  const items = [...menu.querySelectorAll<HTMLElement>('.folder-tab-menu-item')];
  items.forEach(item => {
    item.setAttribute('role', 'menuitem');
    item.tabIndex = 0;
  });
  menu.addEventListener('keydown', event => {
    const index = items.indexOf(menu.ownerDocument.activeElement as HTMLElement);
    if (event.key === 'Escape') {
      event.preventDefault(); menu.remove();
      if (previousFocus?.isConnected) previousFocus.focus();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      items[(index + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length]?.focus();
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); items[index]?.click();
    }
  });
  items[0]?.focus();
}
