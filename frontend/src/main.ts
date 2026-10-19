// Self-hosted @font-face declarations (Newsreader, Plus Jakarta Sans,
// JetBrains Mono, …) ride the main chunk's extracted CSS. Vite content-hashes
// the font binaries; the Python shell preloads the critical UI weights from
// the manifest. Replaces the legacy static/vendor/google-fonts-local.css link.
import './styles/fonts.css';
import {
  getRuntimeService,
  runtimeReady,
  resolveRuntimeAction,
  setRuntimeService,
} from './runtime/app-runtime.js';
import { ready as i18nReady, _onLanguageChange, t } from './i18n';
import { installActionRegistry, registerAction, resolveRegisteredAction } from './action-registry';
import { type FeatureCallable } from './runtime-bridge';
import { connectFeatureRuntime, getFeatureBinding } from './feature-registry';
import {
  ApiError,
  installLegacyApiBindings,
  request,
} from './api/transport';
import { installWorkspaceNavigation, showWorkspace, workspacePath } from './application-shell/page-host';
import { createFeatureLoadRecovery, installFeatureLoadRecovery } from './core/feature-load-recovery';
import { createFeatureModuleCache } from './core/feature-module-cache';
import type { TurnRuntime } from './core/turn-runtime';
import { jumpToPage } from './features/ui-nav';
import { startBootSplash } from './boot-splash';

type DomainModule = {
  prepare?(name: string): Promise<void>;
  invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown>;
};

const settingsEntries = new Set([
  'openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab',
  'jumpToSetting',
  '_oauthLogin',
  'populateToolsInventory', 'searchToolsInventory',
]);
const memoryEntries = new Set([
  'toggleMemory', 'openMemoryModal', 'closeMemoryModal', 'toggleMemoryAddForm',
  'toggleMemoryFromModal', 'refreshPreferences', 'savePreferences',
  '_populatePreferencesTab', 'installSkillFromFileInput', '_openSkillsStoreFromMemory',
]);
const skillsEntries = new Set([
  '_populateSkillsTab', '_skillsSetScope', '_skillsFilter', '_skillsInstallFromInput',
]);
const paperEntries = new Set(['togglePaperMode']);
const paperPreviewEntries = new Set(['_paperPreviewReceipts', '_paperPreviewDiscover', '_paperPreviewOpen']);
const imageEntries = new Set([
  'enterImageGenMode', 'exitImageGenMode', 'generateImageDirect', 'selectIgAspect',
  'selectIgCount', 'selectIgModel', 'selectIgResolution', 'toggleIgModelDropdown',
  '_igCancelGeneration', '_igRetryGenerationTurn',
]);
const projectBrainEntries = new Set([
  'openProjectBrain', 'toggleProjectBrain',
]);
const mydayEntries = new Set(['openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate']);
const utilityPanelEntries = new Set([
  'openUpdateDialog', 'closeUpdateModal', '_renderSettingsUpdatePill',
  'toggleTimerPanel', 'toggleOptimizerPanel',
]);
const knowledgeEntries = new Set([
  'openKnowledgeBase', 'closeKnowledgeBase',
]);
const projectEntries = new Set([
  'openProjectModal', 'closeProjectModal',
]);
const localControlEntries = new Set([
  'openLocalControlModal', 'closeLocalControlModal',
  'toggleBrowserFromLocalModal', 'toggleDesktopFromLocalModal',
  '_lcEnsureAgentRelay',
]);
const diagnosticsPresenterEntries = new Set([
  'toggleDebug', 'closeDebug', 'copyDebugContent',
  'openRequestInspectorForTask', 'openToolDebugPanel',
]);
const compactionViewerEntries = new Set(['openCompactionViewer']);
const slidesEntries = new Set(['openSlidesEditor']);
const miscEntries = new Set([
  'resolveWriteApproval', 'submitStdinInput', 'submitStdinEof',
  'submitHumanGuidanceChoice',
  'submitHumanGuidanceFreeText', 'selectBatchChoice', 'submitBatchAnswers',
  'undoConvModifications', 'undoAllModifications',
  'redoConvModifications', 'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
  '_toggleCostPopover',
]);

const routedFeatureEntries = new Set([
  ...settingsEntries, ...memoryEntries, ...skillsEntries, ...imageEntries,
  ...projectBrainEntries, ...mydayEntries, ...utilityPanelEntries,
  ...knowledgeEntries, ...projectEntries, ...localControlEntries,
  ...diagnosticsPresenterEntries, ...compactionViewerEntries,
  ...miscEntries, ...paperEntries,
  ...paperPreviewEntries, ...slidesEntries,
  'openOrchestration', '_wireConvSyncPush', 'openEvolution',
]);

const domainLoaders = Object.freeze({
  evolution: () => import('./features/evolution'),
  settings: () => import('./features/settings'),
  memory: () => import('./features/memory'),
  skills: () => import('./features/skills'),
  paper: () => import('./features/paper'),
  paperPreview: () => import('./features/paper-preview'),
  image: () => import('./features/image'),
  projectBrain: () => import('./features/project-brain'),
  myday: () => import('./features/myday'),
  utilityPanels: () => import('./features/utility-panels'),
  knowledge: () => import('./features/knowledge'),
  project: () => import('./features/project'),
  localControl: () => import('./features/local-control'),
  diagnosticsPresenters: () => import('./features/diagnostics-presenters'),
  compactionViewer: () => import('./features/compaction-viewer'),
  misc: () => import('./features/misc'),
  slides: () => import('./features/slides'),
  orchestration: () => import('./features/orchestration'),
  infrastructure: () => import('./features/infrastructure'),
});

function domainLoader(name: string): () => Promise<DomainModule> {
  if (name === 'openEvolution') return domainLoaders.evolution;
  if (settingsEntries.has(name)) return domainLoaders.settings;
  if (memoryEntries.has(name)) return domainLoaders.memory;
  if (skillsEntries.has(name)) return domainLoaders.skills;
  if (paperEntries.has(name)) return domainLoaders.paper;
  if (paperPreviewEntries.has(name)) return domainLoaders.paperPreview;
  if (imageEntries.has(name)) return domainLoaders.image;
  if (projectBrainEntries.has(name)) return domainLoaders.projectBrain;
  if (mydayEntries.has(name)) return domainLoaders.myday;
  if (utilityPanelEntries.has(name)) return domainLoaders.utilityPanels;
  if (knowledgeEntries.has(name)) return domainLoaders.knowledge;
  if (projectEntries.has(name)) return domainLoaders.project;
  if (localControlEntries.has(name)) return domainLoaders.localControl;
  if (diagnosticsPresenterEntries.has(name)) {
    return domainLoaders.diagnosticsPresenters;
  }
  if (compactionViewerEntries.has(name)) {
    return domainLoaders.compactionViewer;
  }
  if (miscEntries.has(name)) return domainLoaders.misc;
  if (slidesEntries.has(name)) return domainLoaders.slides;
  if (name === 'openOrchestration') {
    return domainLoaders.orchestration;
  }
  if (name === '_wireConvSyncPush') return domainLoaders.infrastructure;
  throw new Error(`No frontend owner is registered for ${name}`);
}

// Observe boot without covering the shell. The shared send admission stays
// closed until ready; reading and drafting remain available throughout.
startBootSplash({
  request,
  errorStatus: error => (error instanceof ApiError ? error.status : undefined),
  translate: t,
});

// One bounded self-heal for lazy-chunk load failures: the browser module map
// caches a failed dynamic import for the document's lifetime, so only a
// reload can clear it. The pending feature is re-invoked once after boot.
const featureLoadRecovery = createFeatureLoadRecovery({
  now: () => Date.now(),
  readValue: (key: string) => window.sessionStorage.getItem(key),
  writeValue: (key: string, value: string) => {
    window.sessionStorage.setItem(key, value);
  },
  removeValue: (key: string) => { window.sessionStorage.removeItem(key); },
  reload: () => { window.location.reload(); },
  onError: (error: unknown) => {
    console.warn('[modules] feature load recovery error', error);
  },
});

const loadFeatureModule = createFeatureModuleCache();
// Child route graphs share the shell's single bounded reload/replay authority.
installFeatureLoadRecovery(featureLoadRecovery);

const loadDomain = async (name: string): Promise<DomainModule> => {
  try {
    return await loadFeatureModule(domainLoader(name));
  } catch (error: unknown) {
    if (featureLoadRecovery.attemptRecovery(name, error)) {
      // The reload owns the recovery now; never settle so the caller's
      // failure toast cannot race the navigation.
      return new Promise<DomainModule>(() => {});
    }
    throw error;
  }
};

export interface TofuModuleBridge {
  version: 3;
  collectDiagnostics(): Promise<string>;
  preloadBackground(): Promise<void>;
  canInvokeFeature(name: string): boolean;
  prepareFeature(name: string): Promise<void>;
  invokeFeature(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown>;
  resolveAction(name: string): FeatureCallable | undefined;
}

declare global {
  interface Window {
    TofuModules?: TofuModuleBridge;
  }
}

const resolveBinding = (name: string): unknown => (
  resolveRegisteredAction(name) ?? getFeatureBinding(name)
  ?? resolveRuntimeAction(name) ?? getRuntimeService(name)
);

const resolveAction = (name: string): FeatureCallable | undefined => {
  const binding = resolveBinding(name);
  return typeof binding === 'function' ? binding as FeatureCallable : undefined;
};

// The command port is the sole bridge from retained inline/global UI seams to
// their registered domain owners. Missing owners fail closed. Diagnostics is
// a dynamic chunk and does not tax the first screen.
window.TofuModules = Object.freeze({
  version: 3 as const,
  collectDiagnostics: async () => (await import('./features/diagnostics')).collectDiagnostics(),
  preloadBackground: async () => (await import('./features/background')).preload(),
  canInvokeFeature: (name: string) => routedFeatureEntries.has(name),
  prepareFeature: async (name: string) => {
    if (!routedFeatureEntries.has(name)) {
      throw new Error(`No frontend owner is registered for ${name}`);
    }
    const domain = await loadDomain(name);
    await domain.prepare?.(name);
  },
  invokeFeature: async (name: string, args: readonly unknown[], stub: FeatureCallable) => {
    const domain = await loadDomain(name);
    return domain.invoke(name, args, stub);
  },
  resolveAction,
});

connectFeatureRuntime(
  (name: string) => name === 't' ? t : getRuntimeService(name),
  setRuntimeService,
);

// The native agent can open this exact deep link when an authenticated browser
// must carry its SSO-protected polls. Only that entry pays for Local Control at
// boot; ordinary sessions wait for the first explicit panel action.
try {
  if (window.location.hash === '#tofu-agent-relay') {
    void window.TofuModules.prepareFeature('_lcEnsureAgentRelay').catch((error: unknown) => {
      console.warn('[modules] Local Control relay deep link failed', error);
    });
  }
} catch (error) {
  console.warn('[modules] Local Control relay location unavailable', error);
}

registerAction('openEvolution', async () => {
  await loadDomain('openEvolution');
  const open = getFeatureBinding('openEvolution') as (() => void);
  open();
});

/* Page-jump links (`[label](page:<key>)` in assistant markdown) resolve
 * through the eager registry; every target entry point lazy-loads itself. */
registerAction('jumpToPage', jumpToPage);

/* The settings language picker (static/settings_panels/general.html) names
 * `_onLanguageChange('zh'|'en')` in declarative data-tofu-action attributes.
 * Its owner is the i18n module — an import, not a section-owned top-level
 * function — so the generated runtime action table never carried it and the
 * interpreter refused every click ("Unknown action"), leaving the picker
 * dead since the inline-onclick migration. Register it eagerly here. */
registerAction('_onLanguageChange', (language) => {
  if (language === 'zh' || language === 'en') _onLanguageChange(language);
});

installActionRegistry(resolveBinding);

installLegacyApiBindings();

window.dispatchEvent(new CustomEvent('tofu:modules-ready', {
  detail: { version: window.TofuModules.version },
}));

const preloadBackground = (): void => {
  window.TofuModules?.preloadBackground().catch((error: unknown) => {
    console.warn('[modules] background feature preload failed', error);
  });
};
const preloadUtilityPanels = (): void => {
  window.TofuModules?.prepareFeature('openUpdateDialog').catch((error: unknown) => {
    console.warn('[modules] utility panels preload failed', error);
  });
};
const idleCallback = (window as Window & {
  requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
}).requestIdleCallback;
// Idle owners carry subscription/day notices and automatic update checks.
// Routed UI features load through their explicit action or workspace intent.
const preloadIdleFeatures = (): void => {
  preloadBackground();
  preloadUtilityPanels();
};
if (idleCallback) {
  idleCallback(preloadIdleFeatures, { timeout: 5000 });
} else {
  globalThis.setTimeout(preloadIdleFeatures, 2000);
}

Promise.all([i18nReady(), runtimeReady]).then(() => {
  installWorkspaceNavigation(async page => {
    if (page === 'chat') {
      const exit = getFeatureBinding('exitPaperMode');
      if (typeof exit === 'function') (exit as () => void)();
      showWorkspace('chat');
      return;
    }
    const name = page === 'evolution' ? 'openEvolution' : 'togglePaperMode';
    await loadDomain(name);
    if (workspacePath().split('/')[0] !== page) return;
    const open = getFeatureBinding(name);
    if (typeof open === 'function') await (open as () => unknown)();
    showWorkspace(page);
  }, error => console.warn('[workspace] navigation failed', error), page => {
    // HTTP/1 browser sockets are shared with scene assets and API requests.
    // Evolution owns its own status feed; chat resumes from durable cursors.
    const turns = getRuntimeService('ConversationTurnStore') as TurnRuntime | undefined;
    turns?.setTransportSuspended(page === 'evolution');
  });
  window.dispatchEvent(new CustomEvent('tofu:app-ready', {
    detail: { version: window.TofuModules?.version },
  }));
  const pendingFeature = featureLoadRecovery.consumePendingFeature();
  if (pendingFeature && routedFeatureEntries.has(pendingFeature)) {
    // Replay the exact click path: the retained bridge stub owns routing and
    // stub identity, so invoke through the runtime service table.
    const entry = getRuntimeService(pendingFeature);
    if (typeof entry === 'function') {
      try {
        (entry as FeatureCallable)();
      } catch (error: unknown) {
        console.warn('[modules] pending feature resume failed', error);
      }
    }
  }
}).catch((error: unknown) => {
  console.error('[boot] application initialization failed', error);
  window.dispatchEvent(new CustomEvent('tofu:app-failed', { detail: { error } }));
});
