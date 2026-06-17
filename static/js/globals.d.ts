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
declare var updateContextBar: any;    // static/js/context-bar.js — window.updateContextBar
declare var attachCompactionMarkersToConversation: any;  // compaction-viewer.js — window.*
declare var _featureFlags: any;       // index.html inline (var _featureFlags = {})
declare var _markScriptsLoaded: any;  // index.html inline (window._markScriptsLoaded)
declare var Artifacts: any;           // static/js/artifacts.js — window.Artifacts
declare var ConvView: any;            // static/js/conv_view.js — window.ConvView
declare var TradingApp: any;          // static/js/trading/state.js — window.TradingApp
declare var flashGaugeForArchive: any;   // static/js/context-bar.js — window.*
declare var _resolveContextLimit: any;   // static/js/context-bar.js — window.*
declare var openCompactionViewer: any;   // static/js/compaction-viewer.js — window.*
declare var closeCompactionViewer: any;  // static/js/compaction-viewer.js — window.*
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
declare var __translatePushWired: any;   // static/js/translation.js — window.*
declare var ChipInput: any;              // static/js/settings/chip_input.js — window.ChipInput (used in other settings/* files)

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
interface Event { dataTransfer: any; }
// ResizeObserver entry: app reads contentBoxSize[0].inlineSize off the union.
interface ResizeObserverSize { inlineSize: any; blockSize: any; }
// app-specific expando stashed on toast <div>s
interface HTMLDivElement { _dismissed: any; }
// (FileReader.result stays string|ArrayBuffer — call sites coerce via String()
//  since merging can't override an existing property's declared type.)
