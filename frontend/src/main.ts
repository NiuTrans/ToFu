import {
  getRuntimeService,
  loadFeatureFlags,
  runtimeReady,
  resolveRuntimeAction,
  setRuntimeService,
} from './runtime/app-runtime.js';
import { ready as i18nReady, setLanguage, t } from './i18n';
import { installActionRegistry, resolveRegisteredAction } from './action-registry';
import { createLifecycleScope, type LifecycleScope } from './lifecycle';
import { type FeatureCallable } from './runtime-bridge';
import { connectFeatureRuntime, getFeatureBinding } from './feature-registry';
import { normalizeErrorEnvelope } from './api/errors';
import {
  apiTransport,
  installLegacyApiBindings,
  type ApiTransport,
} from './api/transport';
import { formatFileSize } from './core/format-size';
import {
  createAttemptEventStream,
  type AttemptStreamOptions,
  type AttemptStreamConnection,
} from './core/attempt-stream';
import {
  createSendStartupLease,
  type SendStartupLease,
  type SendStartupLeaseOptions,
  type SendStartupOwner,
} from './core/send-startup';
import {
  buildTurnSubmissionExtra,
  buildTurnOperationRequest,
  buildTurnSubmitRequest,
  createTurnCommandId,
  type TurnSubmissionExtra,
  type TurnSubmissionInput,
} from './core/turn-command';
import {
  applyTurnStateProjection,
  projectTurnState,
  turnToLegacyMessage,
  type ApplyTurnProjectionInput,
  type TurnProjectionInput,
  type TurnProjectionResult,
} from './core/turn-projection';
import {
  createTurnStore,
  createTurnState,
  reduceTurnState,
  type ReduceTurnStateOptions,
  type TurnAction,
  type TurnState,
  type TurnStore,
  type TurnStoreOptions,
} from './core/turn-state';
import {
  presentTurnFinish,
  resumeTurnOptions,
  type TurnFinishPresentation,
} from './core/turn-presentation';
import {
  renderTurnStateInto,
  type TurnRenderer,
} from './core/turn-render';
import {
  createConversationTurnRuntime,
  type TurnRuntimeOptions,
} from './core/turn-runtime';

type DomainModule = {
  prepare?(name: string): Promise<void>;
  invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown>;
};

const settingsEntries = new Set([
  'openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab',
]);
const memoryEntries = new Set([
  'toggleMemory', 'openMemoryModal', 'closeMemoryModal', 'toggleMemoryAddForm',
  'toggleMemoryFromModal', 'openMemoryCreateForm', 'refreshPreferences', 'savePreferences',
  '_populatePreferencesTab',
]);
const skillsEntries = new Set(['_populateSkillsTab', '_skillsSetScope', '_skillsFilter']);
const imageEntries = new Set([
  'enterImageGenMode', 'exitImageGenMode', 'generateImageDirect', 'selectIgAspect',
  'selectIgCount', 'selectIgResolution', 'toggleIgModelDropdown',
]);
const projectBrainEntries = new Set([
  'openProjectBrain', 'toggleProjectBrain', 'openProjectBrainInfluence',
]);
const mydayEntries = new Set(['openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate']);
const miscEntries = new Set([
  'openKnowledgeBase', 'closeKnowledgeBase',
  'openProjectModal', 'closeProjectModal', 'resolveWriteApproval',
  'submitStdinInput', 'submitStdinEof', 'submitHumanGuidanceChoice',
  'submitHumanGuidanceFreeText', 'undoConvModifications', 'undoAllModifications',
  'redoConvModifications', 'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
  '_toggleCostPopover', 'openUpdateDialog', 'closeUpdateModal',
  '_renderSettingsUpdatePill', 'toggleTimerPanel', 'toggleOptimizerPanel',
  '_populateToolsTab', '_toolsInvSearch',
]);

const routedFeatureEntries = new Set([
  ...settingsEntries, ...memoryEntries, ...skillsEntries, ...imageEntries,
  ...projectBrainEntries, ...mydayEntries, ...miscEntries,
  'togglePaperMode', 'openOrchestration', 'openTaskMode', '_wireConvSyncPush',
]);

function domainLoader(name: string): () => Promise<DomainModule> {
  if (settingsEntries.has(name)) return () => import('./features/settings');
  if (memoryEntries.has(name)) return () => import('./features/memory');
  if (skillsEntries.has(name)) return () => import('./features/skills');
  if (name === 'togglePaperMode') return () => import('./features/paper');
  if (imageEntries.has(name)) return () => import('./features/image');
  if (projectBrainEntries.has(name)) return () => import('./features/project-brain');
  if (mydayEntries.has(name)) return () => import('./features/myday');
  if (miscEntries.has(name)) return () => import('./features/misc');
  if (name === 'openOrchestration' || name === 'openTaskMode') {
    return () => import('./features/orchestration');
  }
  if (name === '_wireConvSyncPush') return () => import('./features/infrastructure');
  throw new Error(`No frontend owner is registered for ${name}`);
}

export interface TofuModuleBridge {
  version: 3;
  createLifecycleScope(): LifecycleScope;
  loadDiagnostics(): Promise<typeof import('./features/diagnostics')>;
  collectDiagnostics(): Promise<string>;
  attachCookieCaptureConsent(): Promise<
    import('./features/cookie-capture').CookieCaptureConsentController
  >;
  loadDebug(): Promise<typeof import('./features/debug')>;
  apiTransport: ApiTransport;
  loadTurnStoreV2(): Promise<typeof import('./core/turn-store')>;
  normalizeErrorEnvelope: typeof normalizeErrorEnvelope;
  formatFileSize: typeof formatFileSize;
  createAttemptEventStream<TSnapshot = unknown>(
    options: AttemptStreamOptions<TSnapshot>,
  ): AttemptStreamConnection;
  createSendStartupLease(
    owner: SendStartupOwner,
    options?: SendStartupLeaseOptions,
  ): SendStartupLease;
  buildTurnSubmissionExtra(input: TurnSubmissionInput): TurnSubmissionExtra;
  buildTurnSubmitRequest(
    inputTurn: unknown,
    config: unknown,
    extra?: Record<string, unknown> | null,
  ): Record<string, unknown>;
  buildTurnOperationRequest(
    turn: Parameters<typeof buildTurnOperationRequest>[0],
    operation: string,
    config?: unknown,
    options?: Parameters<typeof buildTurnOperationRequest>[3],
  ): Record<string, unknown>;
  createTurnCommandId(): string;
  createConversationTurnRuntime(options: TurnRuntimeOptions): ReturnType<
    typeof createConversationTurnRuntime
  >;
  applyTurnStateProjection(input: ApplyTurnProjectionInput): boolean;
  projectTurnState(input: TurnProjectionInput): TurnProjectionResult;
  turnToLegacyMessage: typeof turnToLegacyMessage;
  presentTurnFinish(
    turn: Parameters<typeof presentTurnFinish>[0],
  ): TurnFinishPresentation | null;
  resumeTurnOptions: typeof resumeTurnOptions;
  renderTurnStateInto(
    container: Element,
    state: TurnState,
    renderTurn?: TurnRenderer,
  ): void;
  createTurnState(conversationId: string): TurnState;
  createTurnStore(
    conversationId: string,
    options?: TurnStoreOptions,
  ): TurnStore;
  reduceTurnState(
    state: TurnState,
    action: TurnAction | null | undefined,
    options?: ReduceTurnStateOptions,
  ): TurnState;
  preloadBackground(): Promise<void>;
  canInvokeFeature(name: string): boolean;
  prepareFeature(name: string): Promise<void>;
  invokeFeature(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown>;
  resolveAction(name: string): FeatureCallable | undefined;
  t: typeof t;
  setLanguage: typeof setLanguage;
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
  createLifecycleScope,
  loadDiagnostics: () => import('./features/diagnostics'),
  collectDiagnostics: async () => (await import('./features/diagnostics')).collectDiagnostics(),
  attachCookieCaptureConsent: async () => (
    await import('./features/cookie-capture')
  ).attachCookieCaptureConsent(),
  loadDebug: () => import('./features/debug'),
  apiTransport,
  loadTurnStoreV2: () => import('./core/turn-store'),
  normalizeErrorEnvelope,
  formatFileSize,
  createAttemptEventStream,
  createSendStartupLease,
  buildTurnSubmissionExtra,
  buildTurnSubmitRequest,
  buildTurnOperationRequest,
  createTurnCommandId,
  createConversationTurnRuntime,
  applyTurnStateProjection,
  projectTurnState,
  turnToLegacyMessage,
  presentTurnFinish,
  resumeTurnOptions,
  renderTurnStateInto,
  createTurnState,
  createTurnStore,
  reduceTurnState,
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
  t,
  setLanguage,
});

connectFeatureRuntime(
  (name: string) => name === 't' ? t : getRuntimeService(name),
  setRuntimeService,
);

installActionRegistry(resolveBinding);

installLegacyApiBindings();
void loadFeatureFlags();

window.dispatchEvent(new CustomEvent('tofu:modules-ready', {
  detail: { version: window.TofuModules.version },
}));

const preloadBackground = (): void => {
  window.TofuModules?.preloadBackground().catch((error: unknown) => {
    console.warn('[modules] background feature preload failed', error);
  });
};
const idleCallback = (window as Window & {
  requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
}).requestIdleCallback;
if (idleCallback) {
  idleCallback(preloadBackground, { timeout: 5000 });
} else {
  globalThis.setTimeout(preloadBackground, 2000);
}

Promise.all([i18nReady(), runtimeReady]).then(() => {
  window.dispatchEvent(new CustomEvent('tofu:app-ready', {
    detail: { version: window.TofuModules?.version },
  }));
}).catch((error: unknown) => {
  console.error('[boot] application initialization failed', error);
  window.dispatchEvent(new CustomEvent('tofu:app-failed', { detail: { error } }));
});
