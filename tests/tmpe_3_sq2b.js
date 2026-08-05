
const { setup } = require(process.env.JSDOM_HARNESS);
const fs = require('fs');
const SRC = process.argv[4];               // path to feature-loader.js (or a mutated copy)  [run_harness: argv[4:]=extra_targets]
const FEATURE_SET = process.argv[5] === '1';
const injected = [];
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [],                             // we eval SRC manually after setting globals
  globals: {
    debugLog: function(){},
    toast: function(){},
  },
});
if (FEATURE_SET) window.__FEATURE_BUNDLE_SRC__ = 'static/js/feature-xyz.js';
// Capture injected <script> tags without loading them; expose .onload/.onerror.
document.head.appendChild = function(node){ injected.push(node); return node; };
// eval the loader source in global scope (window-concat semantics)
(0, eval)(fs.readFileSync(SRC, 'utf8'));
exports.__unused=0; (async () => {

    // Stubs installed for the three deferred entry points.
    check('stub installed openTaskMode', typeof window.openTaskMode === 'function');
    check('stub installed openOrchestration', typeof window.openOrchestration === 'function');
    check('stub installed togglePaperMode', typeof window.togglePaperMode === 'function');

    // Call the stub → should inject the feature bundle <script> exactly once.
    window.openTaskMode('ARG1');
    check('feature script injected once', injected.length === 1);
    check('injected src is feature bundle', injected.length === 1 && /feature-xyz\.js/.test(injected[0].src));

    // Simulate the feature bundle loading: it defines the REAL fn (overwriting
    // the stub) then the <script> onload fires.
    let realArgs = null;
    window.openTaskMode = function(){ realArgs = Array.prototype.slice.call(arguments); };
    if (injected[0].onload) injected[0].onload();
    // The loader's .then() runs on the resolved promise microtask.
    await new Promise(r=>r()); await new Promise(r=>r());
    check('real fn dispatched after load', realArgs && realArgs[0] === 'ARG1');

    // A second call goes straight to the (now real) fn — no new injection.
    const before = injected.length;
    window.openTaskMode('ARG2');
    check('no second injection', injected.length === before);
    report();
    
})();