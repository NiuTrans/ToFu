/** Project location presentation for the unified bar. Server/project_state owns
 * roots and write policy; this view only projects them and invokes its commands.
 */
import { createAnchoredDisclosure } from './disclosure';
type Root = { path: string; readOnly: boolean };
type Translate = (key: string, vars?: Record<string, string | number>) => string;

export const PROJECT_LOCATION_SEP = ' · ';

export function fitProjectLocationNames(
  names: readonly string[],
  available: number,
  measure: (text: string) => number,
  overflowReserve: (count: number) => number = () => 0,
): { label: string; overflow: number } {
  if (!names.length) return { label: '', overflow: 0 };
  const join = (visible: number) => names.slice(0, visible).join(PROJECT_LOCATION_SEP);
  if (!(available > 0) || names.length === 1) {
    return { label: join(names.length), overflow: 0 };
  }
  const fits = (visible: number, extra: number) =>
    measure(join(visible)) + (extra ? overflowReserve(extra) : 0) <= available + 0.5;
  if (fits(names.length, 0)) return { label: join(names.length), overflow: 0 };
  for (let visible = names.length - 1; visible > 1; visible -= 1) {
    const extra = names.length - visible;
    if (fits(visible, extra)) return { label: join(visible), overflow: extra };
  }
  return { label: names[0], overflow: names.length - 1 };
}

export function createProjectContext(ports: {
  bar: HTMLElement;
  t: Translate;
  manage(): void;
  clear(): void;
  openPaper?(): void;
  toggleReadOnly(index: number): void;
}) {
  const { bar } = ports;
  const t = ports.t;
  const doc = bar.ownerDocument;
  const win = doc.defaultView;
  const trigger = bar.querySelector<HTMLButtonElement>('#projectBarFolders')!;
  const auto = bar.querySelector<HTMLButtonElement>('#autoApplyToggle')!;
  const name = trigger.querySelector<HTMLElement>('.project-location-name')!;
  const count = trigger.querySelector<HTMLElement>('.project-location-count')!;
  const panel = doc.createElement('section');
  panel.id = 'projectContextPanel';
  panel.className = 'conversation-bar-panel project-context-panel';
  panel.setAttribute('aria-label', t('conversationBar.projectDetails'));
  const disclosure = createAnchoredDisclosure({ anchor: trigger, panel });
  let roots: readonly Root[] = [];
  let status = '';
  let fingerprint = '';
  let disposed = false;
  let frame = 0;
  const shortName = (path: string): string => path.startsWith('remote:')
    ? path.slice(7) : (path.split(/[\\/]/).filter(Boolean).pop() || path);
  function gapOf(el: HTMLElement): number {
    if (!win) return 0;
    const style = win.getComputedStyle(el);
    return parseFloat(style.columnGap) || parseFloat(style.gap) || 0;
  }
  function occupiedWidth(el: HTMLElement, skip: ReadonlySet<Element>): { width: number; visible: number } {
    let width = 0;
    let visible = 0;
    for (const child of Array.from(el.children)) {
      if (skip.has(child) || (child as HTMLElement).hidden) continue;
      const next = (child as HTMLElement).getBoundingClientRect().width;
      if (next > 0) { width += next; visible += 1; }
    }
    return { width, visible };
  }
  function horizontalPadding(el: HTMLElement): number {
    if (!win) return 0;
    const style = win.getComputedStyle(el);
    return (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
  }
  function nameSlot(): number {
    if (!win) return 0;
    const extras = occupiedWidth(bar, new Set([trigger]));
    const chrome = occupiedWidth(trigger, new Set([name, count]));
    const chromeGaps = extras.visible * gapOf(bar)
      + chrome.visible * gapOf(trigger)
      + horizontalPadding(trigger);
    const fromBar = bar.clientWidth - extras.width - chrome.width - chromeGaps;
    const strip = bar.parentElement;
    if (!strip || strip === bar) return Math.max(0, fromBar);
    const siblings = occupiedWidth(strip, new Set([bar]));
    const fromStrip = strip.clientWidth
      - siblings.width - siblings.visible * gapOf(strip)
      - extras.width - extras.visible * gapOf(bar)
      - chrome.width - chromeGaps;
    // A content-sized bar is circular with the current label. The strip's idle
    // width is the leftover names may grow into.
    return Math.max(0, fromBar, fromStrip);
  }
  function measureLabel(text: string): number {
    if (!win || !text) return 0;
    const probe = doc.createElement('span');
    const cs = win.getComputedStyle(name);
    probe.textContent = text;
    probe.style.cssText = [
      'position:absolute',
      'visibility:hidden',
      'pointer-events:none',
      'display:block',
      'width:max-content',
      'max-width:none',
      'min-width:0',
      'flex:none',
      'overflow:visible',
      'white-space:nowrap',
      `font:${cs.font}`,
      `letter-spacing:${cs.letterSpacing}`,
      `text-transform:${cs.textTransform}`,
      'padding:0',
      'border:0',
      'margin:0',
    ].join(';');
    doc.body.append(probe);
    const width = probe.getBoundingClientRect().width;
    probe.remove();
    return width;
  }
  function overflowReserve(extra: number): number {
    if (!win) return 0;
    const style = win.getComputedStyle(trigger);
    const gap = parseFloat(style.columnGap) || parseFloat(style.gap) || 0;
    const hidden = count.hidden;
    const previous = count.textContent;
    count.hidden = false;
    count.textContent = `+${extra}`;
    const width = count.getBoundingClientRect().width;
    count.hidden = hidden;
    count.textContent = previous;
    return width + gap;
  }
  function paintLocation(): void {
    if (disposed) return;
    const active = roots.length > 0;
    bar.dataset.active = String(active);
    auto.hidden = !active;
    name.removeAttribute('data-i18n');
    trigger.removeAttribute('data-i18n-aria-label');
    trigger.dataset.readOnly = String(active && roots[0].readOnly);
    trigger.title = active ? roots.map(root => root.path).join('\n') : t('conversationBar.attachProject');
    if (!active) {
      name.textContent = t('conversationBar.attachProject');
      count.textContent = '';
      count.hidden = true;
      trigger.setAttribute('aria-label', t('conversationBar.attachProject'));
      return;
    }
    const names = roots.map(root => shortName(root.path));
    let fitted = fitProjectLocationNames(names, nameSlot(), measureLabel, overflowReserve);
    // Chrome the slot math cannot see (strip padding, sibling margins,
    // container-query restyles) can leave the label wider than the share flex
    // actually grants. Refit against the granted width; never let CSS ellipsize
    // a name a shorter fitted label would have shown whole.
    for (let guard = 0; guard < 3; guard += 1) {
      name.textContent = fitted.label;
      count.textContent = fitted.overflow ? `+${fitted.overflow}` : '';
      count.hidden = fitted.overflow < 1;
      if (!win || names.length < 2) break;
      const granted = name.clientWidth;
      if (name.scrollWidth <= granted + 1) break;
      const next = granted > 1
        ? fitProjectLocationNames(names, granted - 1, measureLabel, overflowReserve)
        : { label: names[0], overflow: names.length - 1 };
      if (next.label === fitted.label && next.overflow === fitted.overflow) break;
      fitted = next;
    }
    trigger.setAttribute('aria-label', t('conversationBar.projectLabel', { name: names.join(PROJECT_LOCATION_SEP) }));
  }
  function schedulePaint(): void {
    if (disposed || frame) return;
    if (!win) { paintLocation(); return; }
    frame = win.requestAnimationFrame(() => { frame = 0; if (!disposed) paintLocation(); });
  }
  function renderPanel(): void {
    panel.replaceChildren();
    panel.setAttribute('aria-label', t('conversationBar.projectDetails'));
    const heading = doc.createElement('h3');
    heading.textContent = t('conversationBar.projectDetails');
    panel.append(heading);
    roots.forEach((root, index) => {
      const row = doc.createElement('div');
      row.className = 'project-context-root';
      const path = doc.createElement('span');
      path.className = 'project-context-path';
      path.textContent = root.path;
      const permission = doc.createElement('button');
      permission.type = 'button';
      permission.className = 'project-context-permission';
      permission.textContent = t(root.readOnly ? 'conversationBar.readOnly' : 'conversationBar.writable');
      permission.setAttribute('aria-pressed', String(root.readOnly));
      permission.setAttribute('aria-label', t('conversationBar.toggleReadOnly', { path: root.path }));
      permission.addEventListener('click', () => { disclosure.close(true); ports.toggleReadOnly(index); });
      row.append(path, permission);
      panel.append(row);
    });
    if (status) {
      const note = doc.createElement('p');
      note.className = 'project-context-status';
      note.textContent = status;
      panel.append(note);
    }
    const mode = doc.createElement('button');
    mode.type = 'button';
    mode.className = 'project-context-mode';
    mode.textContent = auto.getAttribute('aria-label') || auto.textContent;
    mode.setAttribute('aria-pressed', auto.getAttribute('aria-pressed') || 'false');
    mode.addEventListener('click', () => { disclosure.close(true); auto.click(); });
    panel.append(mode);
    const actions = doc.createElement('div');
    actions.className = 'conversation-bar-panel-actions';
    for (const [key, action] of [
      ['conversationBar.manageFolders', ports.manage],
      ['conversationBar.detachProject', ports.clear],
    ] as const) {
      const button = doc.createElement('button');
      button.type = 'button';
      button.textContent = t(key);
      button.addEventListener('click', () => { disclosure.close(true); action(); });
      actions.append(button);
    }
    if (ports.openPaper) {
      const open = doc.createElement('button'); open.type = 'button'; open.textContent = t('preview.open');
      open.addEventListener('click', () => { disclosure.close(true); ports.openPaper?.(); });
      actions.prepend(open);
    }
    panel.append(actions);
  }
  function toggle(): void {
    if (disposed) return;
    if (!roots.length) { ports.manage(); return; }
    if (disclosure.isOpen()) disclosure.close(true);
    else { renderPanel(); disclosure.open(); }
  }
  trigger.addEventListener('click', toggle);
  const Observer = win && 'ResizeObserver' in win ? win.ResizeObserver : undefined;
  const resizeObserver = Observer ? new Observer(schedulePaint) : undefined;
  const row = bar.parentElement;
  resizeObserver?.observe(bar);
  if (row && row !== bar) {
    resizeObserver?.observe(row);
    for (const child of Array.from(row.children)) {
      if (child !== bar) resizeObserver?.observe(child);
    }
  }
  const mutationObserver = win && row && row !== bar && 'MutationObserver' in win
    ? new win.MutationObserver((records) => {
      for (const record of records) {
        for (const node of Array.from(record.addedNodes)) {
          if (node.nodeType === 1) resizeObserver?.observe(node as Element);
        }
      }
      schedulePaint();
    }) : undefined;
  if (row && row !== bar) mutationObserver?.observe(row, { childList: true });

  const refreshLanguage = (): void => { fingerprint = ''; view.update({ roots, status }); };
  win?.addEventListener('tofu:language-change', refreshLanguage);
  const view = {
    update(value: { roots: readonly Root[]; status?: string }): void {
      if (disposed) return;
      roots = value.roots;
      status = value.status || '';
      const next = JSON.stringify([roots, status, t('conversationBar.attachProject')]);
      if (next !== fingerprint) { disclosure.close(true); fingerprint = next; }
      paintLocation();
    },
    destroy(): void {
      disposed = true;
      if (frame && win) win.cancelAnimationFrame(frame);
      frame = 0;
      resizeObserver?.disconnect();
      mutationObserver?.disconnect();
      disclosure.destroy(); trigger.removeEventListener('click', toggle);
      win?.removeEventListener('tofu:language-change', refreshLanguage);
    },
  };
  return view;
}
