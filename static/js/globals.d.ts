/**
 * Ambient global declarations for Tofu's vanilla-JS frontend.
 *
 * The frontend loads several vendored libraries via plain <script> tags
 * (see index.html: static/vendor/*.js) and references them as bare
 * globals. They ship no type definitions, so without these `declare`
 * stubs every `katex.`/`marked.`/`pdfjsLib.` reference would be a false
 * "Cannot find name" error that buries the real cross-file bugs this
 * harness exists to catch.
 *
 * Keep these intentionally loose (`any`) — we are NOT trying to type the
 * third-party APIs, only to silence false positives. Real app symbols
 * are NOT declared here; they live in the .js files themselves and TS
 * sees them through the shared global (script) scope.
 */

// ── Vendored libraries (static/vendor/*.js) ──
declare var katex: any;
declare var marked: any;
declare var hljs: any;
declare var DOMPurify: any;
declare var pdfjsLib: any;

// ── Optional/lazily-present globals referenced behind `typeof x !== 'undefined'` ──
declare var mermaid: any;
declare var Chart: any;
declare var html2canvas: any;

// ── App globals attached to window inside IIFEs (not visible as bare
//    script-scope names to tsc) or defined in index.html inline scripts.
//    Declaring them here lets the harness flag GENUINELY-undefined symbols
//    (real typos / stale renames) instead of drowning in these expected
//    cross-boundary references. Keep in sync when a new global surface
//    is added (rare). ──
declare var Api: any;                 // static/js/api.js — global.Api = Api (IIFE)
declare var Icon: any;                // static/js/core/icons.js — window.Icon (SVG factory)
declare var IconDot: any;             // static/js/core/icons.js — window.IconDot (status dot)
declare var updateContextBar: any;    // static/js/context-bar.js — window.updateContextBar
declare var attachCompactionMarkersToConversation: any;  // compaction-viewer.js — window.*
declare var _featureFlags: any;       // index.html inline (var _featureFlags = {})
declare var _markScriptsLoaded: any;  // index.html inline (window._markScriptsLoaded)
declare var Artifacts: any;           // static/js/artifacts.js — window.Artifacts
declare var ConvView: any;            // static/js/conv_view.js — window.ConvView
declare var flashGaugeForArchive: any;   // static/js/context-bar.js — window.*
declare var _resolveContextLimit: any;   // static/js/context-bar.js — window.*
declare var openCompactionViewer: any;   // static/js/compaction-viewer.js — window.*
declare var closeCompactionViewer: any;  // static/js/compaction-viewer.js — window.*
declare var _cvOnLanguageChange: any;    // static/js/compaction-viewer.js — window.* (called by i18n.js _onLanguageChange)
declare var refreshRelayAdminTabs: any;  // static/js/relay-admin.js — window.*
declare var relayAdminCreateUser: any;   // static/js/relay-admin.js — window.*
declare var relayAdminMintCodes: any;    // static/js/relay-admin.js — window.*
declare var relayAdminToggleStatus: any; // static/js/relay-admin.js — window.*
declare var relayAdminTopup: any;        // static/js/relay-admin.js — window.*
declare var _streamRenderNoHighlight: any; // static/js/ui/streaming_ui.js — window.*
declare var _swTimerTicker: any;         // static/js/ui/streaming_ui.js — window.*
declare var _TOFU_DEV_ASSERT: any;       // static/js/ui/turn_nav.js — window.*
declare var _vlmParseEntry: any;         // static/js/upload.js — window.*
declare var _uploadShrinkPolicy: any;    // static/js/main/main_toolbar_ui.js — window.*
declare var _contextPolicy: any;         // static/js/main/main_toolbar_ui.js — window.*
declare var _translationPolicy: any;     // static/js/main/main_toolbar_ui.js — window.*
declare var _browserClientId: any;       // static/js/main/main_toolbar_ui.js — window.*
declare var __sse_test__: any;           // static/js/ui/sse_pipeline.js — window.*
declare var __swarmPushWired: any;       // static/js/ui/swarm_push.js — window.*
declare var presenceRefresh: any;        // static/js/presence.js — window.presenceRefresh
declare var projectBrainRefresh: any;    // static/js/project-brain.js — window.projectBrainRefresh
declare var convInfluenceRefresh: any;   // static/js/project-brain.js — window.convInfluenceRefresh (conv influence bar)
declare var openProjectBrain: any;       // static/js/project-brain.js — window.openProjectBrain (called bare in presence.js)
declare var closeProjectBrain: any;      // static/js/project-brain.js — window.closeProjectBrain
declare var toggleProjectBrain: any;     // static/js/project-brain.js — window.toggleProjectBrain
declare var __translatePushWired: any;   // static/js/translation.js — window.*
declare var ChipInput: any;              // static/js/settings/chip_input.js — window.ChipInput (used in other settings/* files)
declare var buildTurnCtxSnapshot: any;   // static/js/info-rail.js — window.* (used by send pipeline / edit_message)
declare var renderTurnCtxNote: any;      // static/js/info-rail.js — window.* (used by chat_render)
declare var reconcileTurnCtxCapsule: any; // static/js/info-rail.js — window.* (used by sse_pipeline)
declare var refreshMcpRailState: any;    // static/js/info-rail.js — window.* (used by settings/mcp)

// ── Newer-module app globals (2026-07). Attached to window inside IIFEs /
//    exposed as `global.X` (voice.js), so tsc doesn't see them as bare
//    script-scope names — declare them here so cross-file bare references
//    aren't false "Cannot find name" (TS2304). Loose `any`, same as above. ──
declare var initVoiceInput: any;      // static/js/voice.js — global.initVoiceInput (bare in main.js boot)
declare var toggleVoiceInput: any;    // static/js/voice.js — global.toggleVoiceInput (mic onclick)
declare var _welcomePillsHtml: any;   // static/js/core/icons.js — window._welcomePillsHtml (bare in chat_render / main_conv_lifecycle)
declare var toast: any;               // legacy toast() helper, behind `typeof toast === 'function'` (feature-loader.js)

// ── DOM access widening (declaration-merged with lib.dom) ──
//
// The frontend is vanilla DOM JS: `document.getElementById('x').value`,
// `e.target.dataset`, `qs('.y').style`, etc. tsc cannot narrow
// getElementById()'s `HTMLElement` return (or querySelector()'s `Element`,
// or an event's `EventTarget`) to the concrete subtype that actually owns
// `.value` / `.checked` / `.disabled`, so it reported ~400 TS2339s that are
// NOT runtime bugs. Per this harness's stated purpose (tsconfig.json:
// "undefined symbols, typos in global names, wrong argument counts" — NOT
// DOM-property typing), we widen the three base DOM interfaces with the
// form-control + style props the app reads off them, plus the handful of
// app-specific expando properties stashed on DOM nodes. This is a deliberate,
// scoped loosening (NOT a blanket `[key:string]:any`), so genuine
// "Cannot find name" typos for cross-file globals are still flagged.
interface Element {
  value: any; checked: any; disabled: any; selected: any;
  style: any; dataset: any; placeholder: any; title: any;
  hidden: any; open: any; src: any; href: any; onclick: any;
  focus: any; select: any; blur: any; click: any; contentWindow: any;
  files: any; result: any; offsetWidth: any; offsetHeight: any;
  offsetTop: any; offsetParent: any; readOnly: any; type: any;
  // app-specific expando refs stashed on DOM nodes by the renderer
  _msgRef: any; _rawTools: any; _rawMessages: any; _toolsRef: any;
  // _wired: one-time event-wiring latch (main_toolbar_ui.js); _qaCls/_qaSig:
  // paper-reader QA-node className/innerHTML diff cache (paper-reader.js)
  _wired: any; _qaCls: any; _qaSig: any;
  // __bgHtml: chat_render.js background-repaint compare-before-swap cache;
  // selectionStart/End: textarea caret in voice.js _injectText.
  __bgHtml: any; selectionStart: any; selectionEnd: any;
}
// app-specific expando props tsc flags on the concrete HTMLElement subtype
// (getElementById() returns HTMLElement, not Element). Mirror the Element
// expandos it reads there, plus the project-brain one-time wire latches.
interface HTMLElement {
  __bgHtml: any;                            // ui/chat_render.js background-repaint cache
  selectionStart: any; selectionEnd: any;   // voice.js caret insert on #userInput
  _pbTrWired: any;                          // project-brain-i18n.js head toggle wire latch
  _pbPreviewWired: any;                     // project-brain.js hover-preview wire latch
}
interface EventTarget {
  value: any; checked: any; disabled: any; dataset: any;
  closest: any; classList: any; tagName: any; id: any;
  textContent: any; style: any; result: any; files: any;
  getAttribute: any; setAttribute: any; matches: any; parentElement: any;
  error: any; src: any; open: any; querySelector: any;
}
// `this`-typed inline handlers (img.onload = function(){ this.naturalWidth })
// resolve `this` to GlobalEventHandlers; widen with the props read off it.
interface GlobalEventHandlers {
  naturalWidth: any; naturalHeight: any; style: any; checked: any;
  value: any; dataset: any; src: any; width: any; height: any;
}
// Drag events read e.dataTransfer off the base Event type in delegated handlers.
// tofu-pet.js / tofu-scene.js read e.detail off CustomEvents typed as base Event.
interface Event { dataTransfer: any; detail: any; }
// ResizeObserver entry: app reads contentBoxSize[0].inlineSize off the union.
interface ResizeObserverSize { inlineSize: any; blockSize: any; }
// app-specific expando stashed on toast <div>s + finish-info anchor ref + paper-reader tracking
interface HTMLDivElement { _dismissed: any; _anchor: any; _readWords: any; _readTotalMin: any; }
// app-specific expando stashed on a thrown Error (oauth upstream status passthrough)
interface Error { _upstreamStatus: any; }
// app-specific expando properties assigned to `window` inside IIFEs. tsc can't
// see a `window.foo = …` assignment as a declared property of the lib.dom
// `Window` type, so reads of `window.foo` elsewhere are TS2339. These mirror
// the bare-name `declare var` block above for symbols that are ALSO read via
// the explicit `window.` qualifier (relay-admin gates, the reconcile ticker,
// the info-rail capsule helpers). Loose `any` — same rationale as the rest.
interface Window {
  __RELAY_ADMIN_PAGE: any; __RELAY_BILLING_ENABLED: any; __RELAY_MODEL_ENABLED: any;
  relayAdminSwitch: any; relayAdminSaveMargin: any; relayAdminViewPayments: any;
  _cvOnLanguageChange: any;
  _swReconcileTicker: any;
  _timerCountdownTicker: any;  // ui/tool_rounds.js — 1Hz "Next check in Ns" countdown ticker
  buildTurnCtxSnapshot: any; renderTurnCtxNote: any;
  reconcileTurnCtxCapsule: any; refreshMcpRailState: any;
  // mobile_panels.js portaling + flow picker, and the open-flag setters it
  // calls on timer.js / optimizer.js.
  _setTimerPanelOpen: any; _setOptimizerPanelOpen: any;
  openMobileTimer: any; openMobileOptimizer: any; openMobileFlowPicker: any;
  toggleTimerPanel: any; toggleOptimizerPanel: any;
  // presence.js — cross-conversation live-presence strip (one-time wire latch
  // + the conversation-switch re-filter hook).
  __presenceWired: any; presenceRefresh: any; CollabBar: any;
  // project-brain.js — the panel controls + conversation-switch refresh hooks
  //  (all assigned via window.* inside the project-brain IIFE).
  projectBrainRefresh: any;
  toggleProjectBrain: any; openProjectBrain: any; closeProjectBrain: any;
  ProjectBrain: any;
  // project-brain-{peers,status}.js columns + the influence deep-link opener,
  // all assigned via window.* and probed with `typeof window.X !== 'undefined'`.
  ProjectBrainPeers: any; ProjectBrainStatus: any; openProjectBrainInfluence: any;
  // core.js boot/reconnect latches shared across cross_tab_sync.js + main.js,
  // the responsive-breakpoint table, the multi-user id, and the project-modal
  // open flag (main.js / project.js).
  _bootLoadInFlight: any; _bootReconnectStarted: any; _currentUserId: any;
  TOFU_BP: any; _tofuProjectModalOpen: any;
  // cross_tab_sync.js exposes the boot-load-held probe on window; context-bar.js
  // exposes the manual-compaction trigger — both assigned via window.* in IIFEs.
  _isBootLoadHeld: any; runManualCompaction: any;
  isMobileViewport: any; mobileMediaQuery: any; tabletDrawerMediaQuery: any;
  isTabletDrawerViewport: any; isDrawerViewport: any; prefersReducedMotion: any;
  // icons.js welcome-pills html builder (also read via window._welcomePillsHtml).
  _welcomePillsHtml: any;
  // tofu-pet.js / tofu-scene.js — the pet + its scene engine (each window.*-
  // assigns itself and reads the other for the handoff).
  TofuPet: any; TofuScene: any;
  // net-latency.js topbar signal widget init (window.initNetLatency).
  initNetLatency: any;
  // paper-reader.js responsive fold-crossing handler (window.*).
  _paperResponsiveOnCrossing: any;
  // feature-loader.js — deferred-bundle plumbing (all window.* assigned).
  __FEATURE_BUNDLE_SRC__: any; _DEFERRED_ENTRY_POINTS: any;
  _onReady: any; _loadFeatureBundle: any;
}
// (FileReader.result stays string|ArrayBuffer — call sites coerce via String()
//  since merging can't override an existing property's declared type.)
