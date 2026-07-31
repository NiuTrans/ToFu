/* ═══════════════════════════════════════════════════════════════════
   settings/section_requires.js — the degraded-section contract

   THE DEFECT THIS EXISTS FOR (2026-07-29, found by the owner in the live UI):
   the "Brand mascot" block's heading + description are static HTML spliced in
   server-side, while its tiles are painted by JS from core/brand_logo.js. When
   that module isn't in the served bundle (a running server holds the bundle
   manifest it booted with, so a NEW module only appears after a restart), the
   user saw a title, a description, and an EMPTY BOX — a control that looks
   available and does nothing. Project rule: a feature that cannot work must
   not present itself as usable.

   THE CONTRACT (generic — not a one-off patch for the logo picker):
   any settings block whose behaviour depends on a JS symbol declares it:

       <div class="settings-section-needs-js" data-requires="populateWidgetTab">
         …heading / description / control…
         <div class="settings-section-js-missing">此功能需重启服务后生效。</div>
       </div>

   `data-requires` takes one or more space-separated global symbol names. At
   settings-open we check them: all present → the block renders normally and
   the notice stays hidden; any missing → the CONTROLS are hidden and the
   notice is shown, so the failure is visible and self-explanatory instead of
   silent. Adding a new JS-dependent section is one attribute, no new code.

   ── CURRENT CONSUMERS: NONE (2026-07-29) ───────────────────────────
   The brand-mascot picker was the only block declaring `data-requires`, and
   it was removed when the owner settled the mascot question ("I only want the
   original version"). The mechanism is kept deliberately: it is generic, it
   costs one class flip, and the defect it prevents — a JS-painted block
   rendering as an empty box on a server that booted before the module existed
   — is a property of HOW settings blocks are built, not of that one picker.
   The next JS-painted block gets it for free by adding the attribute.
   `applySectionRequirements()` is still called on settings-open for exactly
   that reason: removing the call would make the attribute silently inert, the
   worse of the two failure modes. Its tests therefore drive SYNTHETIC markup
   — honest about having no shipped subject, rather than pointing at a block
   that no longer exists.

   This file is concatenated by lib/js_bundler.py; symbols share the global
   window scope. No exports.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  /** True when every symbol named in `spec` exists as a callable/defined global. */
  function _symbolsPresent(spec) {
    var names = String(spec || '').split(/\s+/).filter(Boolean);
    for (var i = 0; i < names.length; i++) {
      if (typeof window[names[i]] === 'undefined') return false;
    }
    return true;
  }

  /** Apply the degraded-section contract to every declaring block.
   *
   * Called on settings-open (and safe to call repeatedly). Returns the number
   * of sections that ended up degraded — handy for tests / debug output.
   */
  function applySectionRequirements(root) {
    var scope = root || document;
    var blocks = scope.querySelectorAll('.settings-section-needs-js[data-requires]');
    var degraded = 0;
    for (var i = 0; i < blocks.length; i++) {
      var el = blocks[i];
      var ok = _symbolsPresent(el.getAttribute('data-requires'));
      /* `degraded` drives the CSS: it hides the interactive children and
       * reveals the notice. One class flip, no per-section JS. */
      el.classList.toggle('degraded', !ok);
      if (!ok) {
        degraded++;
        if (typeof debugLog === 'function') {
          debugLog('[Settings] section degraded — missing ' +
                   el.getAttribute('data-requires') + ' (stale bundle?)', 'warning');
        }
      }
    }
    return degraded;
  }

  window.applySectionRequirements = applySectionRequirements;
})();
