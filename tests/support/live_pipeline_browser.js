// Passive diagnostics for the actual application's EventSource and DOM.
// Loaded before the app. Native events, reducers and scheduling remain intact.
(() => {
  const rows = {wire: [], dom: [], inputs: [], longTasks: [], opened: [], keydowns: [], eventTimings: [], wheels: [], scrolls: []};
  let conversation = '', turn = '', latest = -1, observer;
  const NativeEventSource = window.EventSource;
  window.EventSource = class extends NativeEventSource {
    constructor(url, options) {
      super(url, options);
      if (!String(url).includes('/conversations/')) return;
      this.addEventListener('open', () => rows.opened.push({url: String(url), at: performance.now()}));
      for (const kind of ['turn.upsert', 'turn.patch', 'turn.deleted', 'attempt.event',
        'conversation.activity', 'sync.heartbeat', 'sync.reset_required']) {
        this.addEventListener(kind, event => {
          if (!conversation || !String(url).includes(`/conversations/${conversation}/`)) return;
          const at = performance.now(), body = JSON.parse(event.data);
          rows.wire.push({at, sequence: body.syncSeq, kind,
            bytes: new TextEncoder().encode(event.data).length,
            markers: [...event.data.matchAll(/PIPELINE_(\d{5})/g)].map(match => Number(match[1]))});
        });
      }
    }
  };
  try {
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) rows.longTasks.push({at: entry.startTime, duration: entry.duration});
    }).observe({type: 'longtask', buffered: true});
  } catch { /* Explicitly report support, never invent a zero population. */ }
  document.addEventListener('input', event => {
    if (event.target?.id !== 'userInput') return;
    const row = {at: performance.now(), value: event.target.value, trusted: event.isTrusted};
    rows.inputs.push(row);
    requestAnimationFrame(() => { row.frame = performance.now(); });
  }, true);
  const nativeTiming = {supported: PerformanceObserver.supportedEntryTypes.includes('event'),
    thresholdMs: 16, durationQuantumMs: 8, droppedEntries: 0, error: null,
    calibrationDurationMs: null};
  let timingObserver;
  const retainTimings = entries => {
    for (const entry of entries) rows.eventTimings.push({name: entry.name, at: entry.startTime,
      duration: entry.duration, processingStart: entry.processingStart, processingEnd: entry.processingEnd,
      interactionId: entry.interactionId, composer: entry.target?.id === 'userInput'});
  };
  if (nativeTiming.supported) {
    try {
      timingObserver = new PerformanceObserver((list, _observer, options) => {
        nativeTiming.droppedEntries += options?.droppedEntriesCount || 0;
        retainTimings(list.getEntries());
      });
      timingObserver.observe({type: 'event', durationThreshold: nativeTiming.thresholdMs});
    } catch (error) { nativeTiming.error = String(error); }
  }
  const keyCount = () => typeof performance.eventCounts?.get === 'function'
    ? performance.eventCounts.get('keydown') || 0 : null;
  document.addEventListener('keydown', event => {
    if (event.target?.id === 'userInput') rows.keydowns.push({at: event.timeStamp,
      handledAt: performance.now(), trusted: event.isTrusted});
  }, true);
  document.addEventListener('wheel', event => {
    if (event.target?.closest?.('#chatContainer')) rows.wheels.push({at: event.timeStamp,
      handledAt: performance.now(), deltaY: event.deltaY, trusted: event.isTrusted,
      topAtHandler: document.querySelector('#chatContainer').scrollTop});
  }, {capture: true, passive: true});
  document.addEventListener('scroll', event => {
    if (event.target?.id !== 'chatContainer') return;
    const row = {at: performance.now(), top: event.target.scrollTop, trusted: event.isTrusted};
    rows.scrolls.push(row);
    requestAnimationFrame(() => { row.frame = performance.now(); });
  }, true);
  window.livePipeline = {
    rows, nativeTiming,
    beginInteractions() {
      nativeTiming.startedAt = performance.now();
      nativeTiming.keyCountBefore = keyCount();
      nativeTiming.interactionCountBefore = performance.interactionCount ?? null;
    },
    endInteractions() {
      retainTimings(timingObserver?.takeRecords() || []);
      nativeTiming.endedAt = performance.now();
      nativeTiming.keyCountAfter = keyCount();
      nativeTiming.interactionCountAfter = performance.interactionCount ?? null;
    },
    start(cid, tid) {
      conversation = cid; turn = tid;
      observer = new MutationObserver(() => {
        const node = document.querySelector(`[data-turn-id="${turn}"]`);
        if (!node) return;
        const matches = [...node.textContent.matchAll(/PIPELINE_(\d{5})/g)];
        if (!matches.length) return;
        const index = Math.max(...matches.map(match => Number(match[1])));
        if (index <= latest) return;
        const at = performance.now();
        for (let i = latest + 1; i <= index; i++) rows.dom.push({index: i, at});
        latest = index;
      });
      observer.observe(document.querySelector('#chatInner'), {subtree: true, childList: true, characterData: true});
    },
    stop() { observer?.disconnect(); },
    longTaskSupported: PerformanceObserver.supportedEntryTypes.includes('longtask'),
  };
})();
