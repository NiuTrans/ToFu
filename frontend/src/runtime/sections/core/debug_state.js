/* ===== migrated source: core/debug_state.js ===== */
/* Thin compatibility port; collection, bounds, and lifecycle are typed. */
const _debugRuntimeOwner = createDebugRuntimeOwner({
  now: () => Date.now(),
  writeConsole: (level, message) => console.log(`[${level}]`, message),
  warnConsole: (message, error) => console.warn(message, error),
  currentUrl: () => location.href,
  userAgent: () => navigator.userAgent,
  conversationCount: () => conversations?.length || 0,
  report: (payload) => Api.clientError.report(payload),
  subscribeError: (listener) => {
    window.addEventListener('error', listener);
    return () => window.removeEventListener('error', listener);
  },
  subscribeUnhandledRejection: (listener) => {
    window.addEventListener('unhandledrejection', listener);
    return () => window.removeEventListener('unhandledrejection', listener);
  },
  resolveClipboardWrite: () => {
    const clipboard = navigator.clipboard;
    return clipboard?.writeText ? (text) => clipboard.writeText(text) : null;
  },
  createClipboardTextarea: () => document.createElement('textarea'),
  appendClipboardTextarea: (textarea) => document.body.appendChild(textarea),
  removeClipboardTextarea: (textarea) => textarea.remove(),
  executeClipboardCopy: () => { document.execCommand('copy'); },
  activeConversationId: () => activeConvId,
  conversations: () => conversations,
  config: () => config,
  visible: () => Boolean(debugVisible),
  setVisible: (value) => { debugVisible = value; },
  readTurnState: (conversationId) =>
    runtimeScope.ConversationTurnRead?.state?.(conversationId),
});
_debugRuntimeOwner.start();
retainedCompositionLifecycle.add(() => _debugRuntimeOwner.dispose());
const debugLog = _debugRuntimeOwner.debugLog;
const _reportClientError = _debugRuntimeOwner.reportClientError;
function _safeClipboardWrite(text) {
  return _debugRuntimeOwner.safeClipboardWrite(text);
}
const _riTaskIdForRound = _debugRuntimeOwner.taskIdForRound;
const DebugShellState = _debugRuntimeOwner.shellState;
runtimeScope.__tofuDiagRing = _debugRuntimeOwner.diagnosticRing;

// Passive diagnostics use the canonical read service and the existing relay.
const _responsivenessInteractions = createBrowserInteractionContext(document, () => performance.now());
retainedCompositionLifecycle.add(() => _responsivenessInteractions.dispose());
const _responsivenessMonitor = startBrowserResponsivenessMonitor({
  now: () => performance.now(),
  wallNow: () => Date.now(),
  schedule: window,
  lifecycle: window,
  interactionContext: (startMs, endMs) => _responsivenessInteractions.context(startMs, endMs),
  timeOriginMs: performance.timeOrigin,
  visibility: document,
  observe: (type, receive) => {
    if (typeof PerformanceObserver === 'undefined'
        || !PerformanceObserver.supportedEntryTypes.includes(type)) return null;
    const observer = new PerformanceObserver((list) => receive(list.getEntries()));
    observer.observe({ type, buffered: false });
    return { disconnect: () => observer.disconnect(), takeRecords: () => observer.takeRecords() };
  },
  context: () => responsivenessContext(activeConvId,
    runtimeScope.ConversationTurnRead?.state?.(activeConvId)),
  report: (message) => {
    console.warn(message);
    runtimeScope.__clientLogRelay?.flush({ urgent: true });
  },
});
retainedCompositionLifecycle.add(() => _responsivenessMonitor.dispose());
