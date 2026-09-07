import { orchestrationRegistry } from './registry';
import { createOrchestrationActionLock } from './action-lock';
import { record, type ContractRecord } from './contracts';
import { createOrchestrationDurableRunCommand } from './durable-run-command';
import { normalizeOrchestrationTaskCreate } from './durable-read';
import { reportOrchestrationDiagnostic } from './diagnostic-report';
import { createOrchestrationEphemeralRunController } from './ephemeral-run';
import { createOrchestrationHumanGateController } from './human-gate-controller';
import { orchestrationMutationMessage } from './mutation-result';
import { orchestrationResultError } from './result';
import { createOrchestrationRunDrawerView } from './run-drawer-view';
import { createOrchestrationRunEventController } from './run-event-controller';
import { createOrchestrationRunPlanCommand } from './run-plan-command';
import { createOrchestrationRunPlanView } from './run-plan-view';
import { createOrchestrationRunRequestClient } from './run-request';
import { normalizeOrchestrationRunPollRead } from './replay-read';
import {
  normalizeOrchestrationMutationRead,
  normalizeOrchestrationPlanRead,
  normalizeOrchestrationRunStart,
} from './runtime-read';
import {
  orchestrationRequestLimitPolicy,
  type OrchestrationRequestLimitPolicy,
} from './request-limits';
import {
  orchestrationRuntimeContractPort,
  projectOrchestrationRuntimeContracts,
  type OrchestrationRuntimeContractPort,
} from './runtime-contracts';

export interface OrchestrationRunControllerOptions extends ContractRecord {
  document?: Document;
  contractPort?: OrchestrationRuntimeContractPort | (() => unknown);
  limitPolicy?: OrchestrationRequestLimitPolicy | unknown;
  requestLimits?: unknown;
  api?: unknown | (() => unknown);
  normalizePlan?: unknown;
  normalizeStart?: unknown;
  normalizePoll?: unknown;
  normalizeMutation?: unknown;
  normalizeTaskCreate?: unknown;
  startSeed?: (definition?: unknown) => unknown;
  onSurfaceChange?: (opened: boolean) => unknown;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  toast?: (message: string, error?: boolean) => unknown;
  onError?: (context: string, error: unknown) => unknown;
  onResetTrace?: () => unknown;
  onStateChange?: (...args: unknown[]) => unknown;
  onGraphChange?: (...args: unknown[]) => unknown;
  onTraceChange?: (...args: unknown[]) => unknown;
  onGateChange?: (...args: unknown[]) => unknown;
  definition?: () => unknown;
  requireValid?: (label: string) => unknown | PromiseLike<unknown>;
  currentId?: () => unknown;
  handoffTaskMode?: (runId: string) => boolean | PromiseLike<boolean>;
  pollDelay?: number | null;
  pollRetryBase?: number | null;
  pollRetryMax?: number | null;
  pollMaxFailures?: number | null;
  setTimeout?: (callback: () => void, delay: number) => number;
  clearTimeout?: (timer: number) => void;
}

type RunControllerWindow = Window & {
  createOrchestrationRunController?: typeof createOrchestrationRunController;
};

/** Studio composition root for plan, ephemeral/durable runs and human gates. */
export function createOrchestrationRunController(
  options: OrchestrationRunControllerOptions = {},
) {
  let fallbackContracts = projectOrchestrationRuntimeContracts(options);
  const contractPort = orchestrationRuntimeContractPort(
    options.contractPort || (() => fallbackContracts));
  const contract = (name: string): unknown => contractPort.get(name);
  const limitPolicy = orchestrationRequestLimitPolicy(
    options.limitPolicy || options.requestLimits);
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value == null ? '' : value)
    : value == null ? '' : value);
  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): string => String(options.translate ? options.translate(key, params) : key);
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const toast = (message: string, isError?: boolean): void => {
    options.toast?.(message, Boolean(isError));
  };
  const report = (context: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.onError, context, error);
  };
  const requests = createOrchestrationRunRequestClient({
    api: options.api,
    normalizePlan: options.normalizePlan ?? normalizeOrchestrationPlanRead,
    normalizeStart: options.normalizeStart ?? normalizeOrchestrationRunStart,
    normalizePoll: options.normalizePoll ?? normalizeOrchestrationRunPollRead,
    normalizeMutation: options.normalizeMutation
      ?? normalizeOrchestrationMutationRead,
    normalizeTaskCreate: options.normalizeTaskCreate
      ?? normalizeOrchestrationTaskCreate,
    mutationContract: () => contract('mutationContract'),
    replayContract: () => contract('replayContract'),
    runContract: () => contract('runContract'),
    runtimeStartContract: () => contract('runtimeStartContract'),
  });
  const drawerView = createOrchestrationRunDrawerView({
    document: options.document ?? document,
    startSeed: options.startSeed,
    translate,
    onVisibilityChange: options.onSurfaceChange,
  });
  let log: (html: string, className?: string) => boolean;
  const planView = createOrchestrationRunPlanView({
    escape,
    translate,
    icon,
    log: (html, className) => log(html, className),
  });
  const actionLock = createOrchestrationActionLock({
    onChange: (action) => { drawerView.setActionState(action); },
  });
  let mutationMessage: (result: unknown, fallback: string) => string;
  const humanGates = createOrchestrationHumanGateController({
    document: options.document ?? document,
    translate,
    icon,
    limitPolicy,
    approve: (requestId, approved) => requests.approve(requestId, approved),
    input: (requestId, value) => requests.input(requestId, value),
    failureMessage: (result, fallback) => mutationMessage(result, fallback),
    report,
    toast,
  });
  const eventController = createOrchestrationRunEventController({
    contract,
    escape,
    translate,
    icon,
    log: (html, className) => log(html, className),
    humanGates,
    onResetTrace: options.onResetTrace,
    onStateChange: options.onStateChange,
    onGraphChange: options.onGraphChange,
    onTraceChange: options.onTraceChange,
    onGateChange: options.onGateChange,
  });
  const definition = (): ContractRecord => record(
    options.definition ? options.definition() : null)
      ?? { nodes: [], edges: [] };
  const runnableSnapshot = (emptyKey: string): ContractRecord | null => {
    const snapshot = definition();
    if (Array.isArray(snapshot.nodes) && snapshot.nodes.length) return snapshot;
    toast(translate(emptyKey), true);
    return null;
  };
  const permits = async (labelKey: string): Promise<boolean> =>
    !options.requireValid
      || Boolean(await options.requireValid(translate(labelKey)));
  const clearLog = (): void => {
    drawerView.clearLog();
    humanGates.clearAll();
  };
  log = (html, className) => drawerView.log(html, className);
  const resetTrace = (): void => { eventController.reset(); };
  mutationMessage = (result, fallback) => {
    const response = record(result)?.response;
    return orchestrationMutationMessage(
      response, translate, fallback, contract('mutationContract'));
  };
  const renderEvent = (event: unknown): void => {
    eventController.render(record(event) ?? {});
  };
  const ephemeralRun = createOrchestrationEphemeralRunController({
    actionLock,
    requests,
    permits: () => permits('orch.run.testRun'),
    clearLog,
    resetTrace,
    renderEvent,
    mutationMessage,
    resultError: orchestrationResultError,
    report,
    log,
    translate,
    escape,
    icon,
    pollDelay: options.pollDelay,
    pollRetryBase: options.pollRetryBase,
    pollRetryMax: options.pollRetryMax,
    pollMaxFailures: options.pollMaxFailures,
    setTimeout: options.setTimeout,
    clearTimeout: options.clearTimeout,
  });
  const planCommand = createOrchestrationRunPlanCommand({
    actionLock,
    requests,
    runnableSnapshot,
    permits,
    clearLog,
    view: planView,
    report,
    translate,
    resultError: orchestrationResultError,
  });
  const durableRun = createOrchestrationDurableRunCommand({
    actionLock,
    requests,
    runnableSnapshot,
    inputFor: (snapshot) => drawerView.inputFor(snapshot),
    permits,
    currentId: options.currentId,
    handoff: options.handoffTaskMode,
    toast,
    translate,
    report,
    resultError: orchestrationResultError,
  });
  const run = async (): Promise<boolean> => {
    if (actionLock.pending()) return false;
    const snapshot = runnableSnapshot('orch.run.nothingToRun');
    if (!snapshot) return false;
    return ephemeralRun.run(snapshot, drawerView.inputFor(snapshot));
  };

  return {
    state: eventController.state,
    traceSnapshotFor: eventController.traceSnapshotFor,
    traceFor: eventController.traceFor,
    traceHistoryFor: eventController.traceHistoryFor,
    traceCountFor: eventController.traceCountFor,
    isBusy: () => actionLock.pending('run'),
    isActionBusy: actionLock.pending,
    isOpen: drawerView.isOpen,
    open: drawerView.open,
    close: drawerView.close,
    log,
    clearLog,
    resetTrace,
    setEventContract: (eventContract: unknown): unknown => {
      fallbackContracts = projectOrchestrationRuntimeContracts({
        ...fallbackContracts,
        eventContract,
      });
      return fallbackContracts.eventContract;
    },
    setRuntimeContracts: (contracts: unknown): ContractRecord => {
      fallbackContracts = projectOrchestrationRuntimeContracts(contracts);
      return fallbackContracts;
    },
    renderEvent,
    humanApprove: humanGates.approve,
    humanInput: humanGates.input,
    plan: planCommand.run,
    run,
    runAsTask: durableRun.run,
    abort: ephemeralRun.abort,
  };
}

(orchestrationRegistry as unknown as RunControllerWindow).createOrchestrationRunController =
  createOrchestrationRunController;
