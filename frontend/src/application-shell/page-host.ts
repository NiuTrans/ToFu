/** Workspace chrome and bounded mount lifecycle. Features own only their page subtree.
 * Entry points: showWorkspace, releaseWorkspace, installWorkspaceNavigation.
 * Dependencies: injected navigation loader and existing shell DOM; no feature imports.
 */
export type Workspace = 'chat' | 'paper' | 'evolution';
let active: Workspace = 'chat';
let disposePage: (() => void) | undefined;
let routePage: ((path: string) => void) | undefined;
let navigate: ((page: Workspace) => Promise<void>) | undefined;
let workspaceChanged: ((page: Workspace) => void) | undefined;

export function workspacePath(): string {
  return window.location.hash.startsWith('#/') ? window.location.hash.slice(2) : '';
}

export function showWorkspace(page: Workspace, updateAddress = true): void {
  if (active !== page) { disposePage?.(); disposePage = undefined; routePage = undefined; }
  active = page;
  workspaceChanged?.(page);
  document.body.dataset.workspace = page;
  for (const [name, id] of [['paper', 'paperModeContainer'], ['evolution', 'evolutionPageContainer']]) {
    const element = document.getElementById(id);
    if (element) element.style.display = name === page ? 'flex' : 'none';
  }
  for (const selector of ['.chat-wrapper', '.input-area']) {
    const element = document.querySelector<HTMLElement>(selector);
    if (element) element.style.display = page === 'chat' ? '' : 'none';
  }
  document.querySelectorAll<HTMLAnchorElement>('[data-workspace-link]').forEach(link => {
    if (link.dataset.workspaceLink === page) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  const addressedWorkspace = workspacePath().split('/')[0] || 'chat';
  if (updateAddress && addressedWorkspace !== page) {
    // Direct reader commands retain their arguments; do not re-enter on a hash event.
    window.history.pushState(null, '', `#/${page}`);
  }
}

export function releaseWorkspace(page: Workspace): void {
  if (active === page) showWorkspace('chat', workspacePath().split('/')[0] === page);
}

export function mountWorkspacePage(dispose: () => void, updateRoute: (path: string) => void): void {
  disposePage?.();
  disposePage = dispose;
  routePage = updateRoute;
}

/** Mount authority comes from the shell lifetime, never a page's incidental markup. */
export function hasMountedWorkspace(page: Workspace): boolean {
  return active === page && disposePage !== undefined;
}

export function goToWorkspace(path: string): void {
  if (workspacePath() === path) return;
  window.location.hash = `/${path}`;
}

export function installWorkspaceNavigation(
  loader: (page: Workspace) => Promise<void>,
  onError: (error: unknown) => void,
  onWorkspaceChange?: (page: Workspace) => void,
): () => void {
  navigate = loader;
  workspaceChanged = onWorkspaceChange;
  workspaceChanged?.(active);
  let pendingPath: string | undefined;
  const changed = () => {
    const path = workspacePath();
    const name = path.split('/')[0] || 'chat';
    // Retired standalone research mode: deep links land on its successor
    // page inside the Evolution workspace.
    if (name === 'research') {
      window.location.hash = `/evolution/${path}`;
      return;
    }
    if (!['chat', 'paper', 'evolution'].includes(name)) return;
    // Release hidden-workspace resources before lazy imports compete for sockets.
    workspaceChanged?.(name as Workspace);
    if (name === active) { routePage?.(path); return; }
    if (pendingPath === path) return;
    pendingPath = path;
    void navigate?.(name as Workspace).catch(error => {
      if (pendingPath === path) workspaceChanged?.(active);
      onError(error);
    }).finally(() => {
      if (pendingPath === path) pendingPath = undefined;
    });
  };
  window.addEventListener('hashchange', changed);
  window.addEventListener('popstate', changed);
  changed();
  return () => {
    window.removeEventListener('hashchange', changed);
    window.removeEventListener('popstate', changed);
    disposePage?.(); disposePage = undefined; routePage = undefined; navigate = undefined;
    workspaceChanged = undefined;
  };
}
