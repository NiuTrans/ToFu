import { orchestrationRegistry } from './registry';
type Port = Record<string, unknown>;

export interface TaskModeServicesOptions extends Port {
  document?: unknown;
  window?: unknown;
  translate?: (key: string, params?: unknown) => unknown;
  escape?: (value: unknown) => unknown;
  richCopy?: (value: unknown) => unknown;
  iconSrc?: (name: string) => unknown;
  hidden?: () => unknown;
}

type TaskModeServicesWindow = Window & {
  resolveOrchestrationApiClient?: () => unknown;
  _orchStudioApi?: Port | null;
  t?: (key: string, params?: unknown) => unknown;
  escapeHtml?: (value: unknown) => unknown;
  formatOrchestrationRichCopy?: (value: unknown) => unknown;
  showToast?: (message: unknown, kind: string) => unknown;
  showConfirm?: (message: unknown, config: unknown) => unknown;
  _orchServices?: Port | null;
  _ORCH_ICONS?: Port;
  _ORCH_ROLES?: unknown[];
  _ORCH_CONTROLS?: unknown[];
  _ORCH_GLYPHS?: Port;
  _orchIconSrc?: (name: string) => unknown;
  createTaskModeServices?: typeof createTaskModeServices;
  _tmServices?: ReturnType<typeof createTaskModeServices>;
};

/** Late-bound browser/application capabilities used by Task Mode. */
export function createTaskModeServices(options: TaskModeServicesOptions = {}) {
  const doc = options.document
    ?? (typeof document !== 'undefined' ? document : null);
  const win = options.window
    ?? (typeof window !== 'undefined' ? window : null);
  const provided = (name: string, fallback: unknown): unknown => {
    const provider = options[name];
    if (typeof provider === 'function') return (provider as () => unknown)();
    return provider == null ? fallback : provider;
  };
  const translate = (key: string, params?: unknown): unknown =>
    typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  const escape = (value: unknown): unknown =>
    typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  const richCopy = (value: unknown): unknown =>
    typeof options.richCopy === 'function'
      ? options.richCopy(value) : escape(value);
  const call = (name: string, fallback: unknown, ...args: unknown[]): unknown => {
    const callback = options[name];
    return typeof callback === 'function'
      ? (callback as (...values: unknown[]) => unknown).apply(null, args)
      : fallback;
  };

  return Object.freeze({
    document: doc,
    window: win,
    api: () => {
      const resolver = (orchestrationRegistry as unknown as TaskModeServicesWindow)
        .resolveOrchestrationApiClient;
      return provided('api', typeof resolver === 'function' ? resolver() : null);
    },
    studio: () => provided('studio', null),
    translate,
    escape,
    richCopy,
    toast: (message: unknown, isError?: unknown) =>
      call('toast', null, message, Boolean(isError)),
    confirm: (message: unknown, config?: unknown) =>
      call('confirm', true, message, config),
    reportError: (scope: unknown, context: unknown, error: unknown) =>
      call('reportError', undefined, scope, context, error),
    hidden: () => typeof options.hidden === 'function'
      ? Boolean(options.hidden())
      : Boolean(doc && typeof doc === 'object' && (doc as Port).hidden),
    icon: (name: string): unknown => {
      const icons = provided('icons', {});
      return icons && typeof icons === 'object'
        ? (icons as Port)[name] || '' : '';
    },
    roles: () => provided('roles', []),
    controls: () => provided('controls', []),
    glyphs: () => provided('glyphs', {}),
    iconSrc: (name: string): unknown => typeof options.iconSrc === 'function'
      ? options.iconSrc(name) : '',
  });
}

const bridge = orchestrationRegistry as unknown as TaskModeServicesWindow;
bridge.createTaskModeServices = createTaskModeServices;
bridge._tmServices = createTaskModeServices({
  document: typeof document !== 'undefined' ? document : null,
  window: typeof window !== 'undefined' ? window : null,
  studio: () => bridge._orchStudioApi || null,
  translate: (key: string, params?: unknown) =>
    typeof bridge.t === 'function' ? bridge.t(key, params) : key,
  escape: (value: unknown) => typeof bridge.escapeHtml === 'function'
    ? bridge.escapeHtml(value == null ? '' : value)
    : String(value == null ? '' : value),
  richCopy: (value: unknown) => {
    if (typeof bridge.formatOrchestrationRichCopy === 'function') {
      return bridge.formatOrchestrationRichCopy(value);
    }
    return typeof bridge.escapeHtml === 'function'
      ? bridge.escapeHtml(value == null ? '' : value)
      : String(value == null ? '' : value);
  },
  toast: (message: unknown, isError: unknown) => {
    const studio = bridge._orchStudioApi;
    if (studio && typeof studio.toast === 'function') {
      return (studio.toast as (...values: unknown[]) => unknown)(
        message, isError);
    }
    return typeof bridge.showToast === 'function'
      ? bridge.showToast(message, isError ? 'error' : 'info') : null;
  },
  confirm: (message: unknown, config: unknown) =>
    typeof bridge.showConfirm === 'function'
      ? bridge.showConfirm(message, config) : true,
  reportError: (scope: unknown, context: unknown, error: unknown) => {
    const services = bridge._orchServices;
    if (services && typeof services.reportError === 'function') {
      return (services.reportError as (...values: unknown[]) => unknown)(
        scope, context, error);
    }
    return console?.warn?.(
      `[${String(scope || 'TaskMode')}] ${String(context || 'operation')} failed:`,
      error,
    );
  },
  icons: () => bridge._ORCH_ICONS || {},
  roles: () => bridge._ORCH_ROLES || [],
  controls: () => bridge._ORCH_CONTROLS || [],
  glyphs: () => bridge._ORCH_GLYPHS || {},
  iconSrc: (name: string) => typeof bridge._orchIconSrc === 'function'
    ? bridge._orchIconSrc(name) : '',
});
