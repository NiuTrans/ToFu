/** Evolution route lifetime: one mounted page, latest pending path and stale-load fencing.
 * Entry: createEvolutionRouteHost. Dependencies: injected page loaders/status/recovery.
 * Imports own their document-lifetime cache outside this host; disposal owns no timer.
 */
export type EvolutionPageKind = 'programs' | 'studio' | 'research' | 'detail';
export interface MountedEvolutionPage { updateRoute?(path: string): void; dispose(): void }
export type MountEvolutionPage = (root: HTMLElement, path: string) => MountedEvolutionPage;
export interface EvolutionRouteHostPorts {
  root: HTMLElement;
  loadPage(kind: EvolutionPageKind): Promise<MountEvolutionPage>;
  showLoading(): void;
  showFailure(error: unknown): void;
  recoverFailure?(error: unknown): boolean;
  reportFailure(error: unknown): void;
}

export function evolutionPageKind(path: string): EvolutionPageKind {
  const segment = path.split('/')[1] || '';
  if (segment === 'research') return 'research';
  if (!segment || segment === 'programs') return 'programs';
  return ['runs', 'start'].includes(segment) ? 'studio' : 'detail';
}

export function createEvolutionRouteHost(ports: EvolutionRouteHostPorts, initialPath: string) {
  let page: MountedEvolutionPage | null = null;
  let kind: EvolutionPageKind | null = null;
  let latestPath = initialPath;
  let generation = 0;
  let disposed = false;
  const current = (epoch: number) => !disposed && epoch === generation;
  const report = (error: unknown) => {
    try { ports.reportFailure(error); } catch { /* diagnostics never own teardown */ }
  };
  function releasePage(): void {
    const previous = page; page = null;
    try { previous?.dispose(); } catch (error) { report(error); }
  }
  function updateRoute(path: string): void {
    if (disposed) return;
    latestPath = path;
    const next = evolutionPageKind(path);
    if (next === kind) { page?.updateRoute?.(path); return; }
    const epoch = ++generation;
    releasePage(); kind = next;
    ports.root.replaceChildren();
    ports.root.classList.remove('evo-workspace', 'evo-studio', 'evo-programs');
    ports.root.setAttribute('aria-busy', 'true');
    ports.showLoading();
    void Promise.resolve().then(() => current(epoch) ? ports.loadPage(next) : null).then(mount => {
      if (!mount || !current(epoch)) return;
      const mounted = mount(ports.root, latestPath);
      if (!current(epoch)) {
        try { mounted.dispose(); } catch (error) { report(error); }
        return;
      }
      page = mounted;
      ports.root.removeAttribute('aria-busy');
    }).catch(error => {
      if (!current(epoch)) return;
      releasePage(); ports.root.removeAttribute('aria-busy');
      report(error);
      let recovering = false;
      try { recovering = Boolean(ports.recoverFailure?.(error)); }
      catch (recoveryError) { report(recoveryError); }
      if (!recovering) {
        try { ports.showFailure(error); } catch (viewError) { report(viewError); }
      }
    });
  }
  updateRoute(initialPath);
  return {
    updateRoute,
    dispose(): void {
      if (disposed) return;
      disposed = true; generation += 1; releasePage();
      ports.root.replaceChildren(); ports.root.removeAttribute('aria-busy');
    },
  };
}
