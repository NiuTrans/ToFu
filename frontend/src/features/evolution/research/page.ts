/** Research surface owner — the auto-research workbench is a page inside the
 * Evolution workspace (`#/evolution/research`), not a shell mode. Direction
 * composer, live pipeline console, record archive and production workspace all
 * render here; the server-side job stays resumable while the page is away.
 *
 * Owns: the page subtree markup (console host + archive rail), attach/resume
 * on mount, and client-only teardown on dispose. Registration of the shared
 * `_research*` globals stays with the self-registering modules imported here.
 */
import { featureRegistry } from '../../../feature-registry';
import { escapeHtml } from '../../../html-safety';
import { t } from '../../../i18n';
import './components/view';
import './controllers/runtime';
import './controllers/workspace';
import { openResearchReader } from './api/reader';
import { destroyResearchWorkspace } from './controllers/workspace';
import { evolutionRunsApi } from '../../../api/evolution-runs';
import '../../../styles/application/27-evolution-research.css';

type ResearchRegistry = Window & Record<string, any> & {
  _researchStream?: unknown;
  _paintResearch?: () => void;
  _showResearchLanding?: () => void;
  _renderResearchSidebar?: () => Promise<void>;
  _resumeInflightResearch?: () => Promise<boolean> | boolean;
  _destroyResearchRuntime?: () => void;
};

function registry(): ResearchRegistry {
  return featureRegistry as unknown as ResearchRegistry;
}

(featureRegistry as unknown as Record<string, unknown>)._researchReader = openResearchReader;
// The research workbench launches its pipeline as a research-domain evolution
// run; the composition root wires the typed client, controllers stay seam-driven.
(featureRegistry as unknown as Record<string, unknown>)._evolutionRunsApi = evolutionRunsApi;

export function mountResearchPage(root: HTMLElement): { dispose(): void } {
  const state = registry();
  const recordsTitle = escapeHtml(t('evolution.research.recordsTitle'));
  root.innerHTML = '<div class="evo-research-shell">'
    + '<aside class="evo-research-rail" aria-label="' + recordsTitle + '">'
    + '<div class="research-sidebar-current" id="researchSidebarCurrent"></div>'
    + '<div class="research-sidebar-lib-header">'
    + '<span>' + recordsTitle + '</span>'
    + '<div style="display:flex;align-items:center;gap:6px;">'
    + '<span class="research-sidebar-lib-count" id="researchRecordCount"></span>'
    + '<button class="research-sidebar-new-inline-btn" data-tofu-action="_newResearchDirection()" title="' + escapeHtml(t('evolution.research.newDirection')) + '">'
    + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14"/><path d="M5 12h14"/></svg>'
    + '</button></div></div>'
    + '<div class="research-record-list" id="researchRecordList"></div>'
    + '</aside>'
    + '<div class="research-body" id="researchViewer"></div>'
    + '</div>';

  let disposed = false;
  void state._renderResearchSidebar?.();
  if (state._researchStream) {
    state._paintResearch?.();
  } else {
    state._showResearchLanding?.();
    // A previously launched job keeps running server-side while the user is
    // elsewhere; re-attach instead of stranding them on the landing page.
    void Promise.resolve(state._resumeInflightResearch?.() ?? false)
      .then((resumed: boolean) => {
        if (!resumed || disposed) return;
        state._paintResearch?.();
      });
  }

  return {
    dispose(): void {
      disposed = true;
      destroyResearchWorkspace();
      state._destroyResearchRuntime?.();
      root.replaceChildren();
    },
  };
}
