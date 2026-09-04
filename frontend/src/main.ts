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
import { ready as i18nReady, t } from './i18n';
import { installActionRegistry, resolveRegisteredAction } from './action-registry';
import { type FeatureCallable } from './runtime-bridge';
import { connectFeatureRuntime, getFeatureBinding } from './feature-registry';
import {
  installLegacyApiBindings,
} from './api/transport';

type DomainModule = {
  prepare?(name: string): Promise<void>;
  invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown>;
};

interface TofuSceneRuntimeBridge {
  readonly BASE_PATH: string;
  readonly t: typeof t;
  TofuPet: unknown;
  TofuScene: unknown;
}

const settingsEntries = new Set([
  'openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab',
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
const paperEntries = new Set(['togglePaperMode', 'toggleResearchMode']);
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
const miscEntries = new Set([
  'resolveWriteApproval', 'submitStdinInput', 'submitStdinEof',
  'submitHumanGuidanceChoice',
  'submitHumanGuidanceFreeText', 'undoConvModifications', 'undoAllModifications',
  'redoConvModifications', 'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
  '_toggleCostPopover',
]);

const routedFeatureEntries = new Set([
  ...settingsEntries, ...memoryEntries, ...skillsEntries, ...imageEntries,
  ...projectBrainEntries, ...mydayEntries, ...utilityPanelEntries,
  ...knowledgeEntries, ...projectEntries, ...localControlEntries,
  ...diagnosticsPresenterEntries, ...compactionViewerEntries,
  ...miscEntries, ...paperEntries,
  'openOrchestration', 'openTaskMode', '_wireConvSyncPush',
]);

function domainLoader(name: string): () => Promise<DomainModule> {
  if (settingsEntries.has(name)) return () => import('./features/settings');
  if (memoryEntries.has(name)) return () => import('./features/memory');
  if (skillsEntries.has(name)) return () => import('./features/skills');
  if (paperEntries.has(name)) return () => import('./features/paper');
  if (imageEntries.has(name)) return () => import('./features/image');
  if (projectBrainEntries.has(name)) return () => import('./features/project-brain');
  if (mydayEntries.has(name)) return () => import('./features/myday');
  if (utilityPanelEntries.has(name)) return () => import('./features/utility-panels');
  if (knowledgeEntries.has(name)) return () => import('./features/knowledge');
  if (projectEntries.has(name)) return () => import('./features/project');
  if (localControlEntries.has(name)) return () => import('./features/local-control');
  if (diagnosticsPresenterEntries.has(name)) {
    return () => import('./features/diagnostics-presenters');
  }
  if (compactionViewerEntries.has(name)) {
    return () => import('./features/compaction-viewer');
  }
  if (miscEntries.has(name)) return () => import('./features/misc');
  if (name === 'openOrchestration' || name === 'openTaskMode') {
    return () => import('./features/orchestration');
  }
  if (name === '_wireConvSyncPush') return () => import('./features/infrastructure');
  throw new Error(`No frontend owner is registered for ${name}`);
}

export interface TofuModuleBridge {
  version: 3;
  collectDiagnostics(): Promise<string>;
  preloadBackground(): Promise<void>;
  canInvokeFeature(name: string): boolean;
  prepareFeature(name: string): Promise<void>;
  invokeFeature(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown>;
  resolveAction(name: string): FeatureCallable | undefined;
  sceneRuntime: TofuSceneRuntimeBridge;
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

// Decorative scene chunks have a deliberately narrow mutable port. They can
// coordinate with one another without receiving the retained runtime object or
// publishing another browser global.
const sceneRuntime: TofuSceneRuntimeBridge = Object.seal({
  get BASE_PATH(): string {
    const value = getRuntimeService('BASE_PATH');
    return typeof value === 'string' ? value : '';
  },
  get t(): typeof t {
    return t;
  },
  get TofuPet(): unknown {
    return getRuntimeService('TofuPet');
  },
  set TofuPet(value: unknown) {
    setRuntimeService('TofuPet', value);
  },
  get TofuScene(): unknown {
    return getRuntimeService('TofuScene');
  },
  set TofuScene(value: unknown) {
    setRuntimeService('TofuScene', value);
  },
});

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
    const domain = await domainLoader(name)();
    await domain.prepare?.(name);
  },
  invokeFeature: async (name: string, args: readonly unknown[], stub: FeatureCallable) => {
    const domain = await domainLoader(name)();
    return domain.invoke(name, args, stub);
  },
  resolveAction,
  sceneRuntime,
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
const preloadIdleFeatures = (): void => {
  preloadBackground();
  preloadUtilityPanels();
};
const idleCallback = (window as Window & {
  requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
}).requestIdleCallback;
if (idleCallback) {
  idleCallback(preloadIdleFeatures, { timeout: 5000 });
} else {
  globalThis.setTimeout(preloadIdleFeatures, 2000);
}

const loadAmbientScene = async (): Promise<void> => {
  // The pet owns the selected decor, so establish it before the canvas reads
  // that state. Each ornament fails soft independently after application boot.
  try {
    await import('./runtime/scene/tofu-pet.js');
  } catch (error) {
    console.warn('[modules] ambient pet preload failed', error);
  }
  try {
    await import('./runtime/scene/tofu-scene.js');
  } catch (error) {
    console.warn('[modules] ambient scene preload failed', error);
  }
};

const scheduleAmbientScene = (): void => {
  const load = (): void => { void loadAmbientScene(); };
  if (idleCallback) {
    idleCallback(load, { timeout: 4000 });
  } else {
    globalThis.setTimeout(load, 1200);
  }
};

Promise.all([i18nReady(), runtimeReady]).then(() => {
  window.dispatchEvent(new CustomEvent('tofu:app-ready', {
    detail: { version: window.TofuModules?.version },
  }));
  scheduleAmbientScene();
}).catch((error: unknown) => {
  console.error('[boot] application initialization failed', error);
  window.dispatchEvent(new CustomEvent('tofu:app-failed', { detail: { error } }));
});
