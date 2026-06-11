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
