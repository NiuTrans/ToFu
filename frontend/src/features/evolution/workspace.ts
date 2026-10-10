/** Lazy Evolution page composition. Only the selected route imports its graph.
 * Entry: mountEvolutionWorkspace. Dependencies: route-host, shared module cache,
 * shared UI/i18n; page types are erased. Studio controls load with studio-entry.
 */
import type { mountEvolution } from './view';
import type { mountProgramWorkbench } from './programs/page';
import { createFeatureModuleCache } from '../../core/feature-module-cache';
import { createAlert, createButton } from '../../ui';
import { t } from '../../i18n';
import { createEvolutionRouteHost, type EvolutionPageKind, type MountedEvolutionPage } from './route-host';

type WorkspacePorts = Parameters<typeof mountEvolution>[1]
  & Pick<Parameters<typeof mountProgramWorkbench>[1], 'draftOwner' | 'draftScope' | 'draftStorage'>
  & { recoverLoadFailure?: (error: unknown) => boolean };
const loadPageModule = createFeatureModuleCache();
const pageLoaders: Record<EvolutionPageKind,
  () => Promise<(root: HTMLElement, ports: WorkspacePorts) => MountedEvolutionPage>> = {
  programs: () => import('./programs/page').then(module => module.mountProgramWorkbench),
  studio: () => import('./studio-entry').then(module => module.mountStudio),
  detail: () => import('./view').then(module => module.mountEvolution),
  research: () => import('./research/page').then(module => module.mountResearchPage),
};

export function mountEvolutionWorkspace(root: HTMLElement, ports: WorkspacePorts) {
  const loadPage = async (kind: EvolutionPageKind) => {
    const mount = await loadPageModule(pageLoaders[kind]);
    return (target: HTMLElement, path: string) => mount(target, { ...ports, path });
  };
  return createEvolutionRouteHost({
    root, loadPage,
    showLoading: () => {
      root.classList.add('evo-workspace');
      root.append(createAlert({ variant: 'info', title: t('evolution.page.loading') }).el);
    },
    showFailure: () => {
      root.replaceChildren(createAlert({ variant: 'error', title: t('evolution.page.loadFailed') }).el,
        createButton({ label: t('evolution.page.reload'), onClick: () => location.reload() }, root.ownerDocument));
    },
    recoverFailure: ports.recoverLoadFailure,
    reportFailure: error => console.warn('[evolution] page lifecycle failed', error),
  }, ports.path);
}
