/* ===== migrated source: core/brand_logo.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/brand_logo.js — THE single source of the brand mascot URL

   CACHE-BUST is the whole job. Every reference to the welcome mascot used to
   be a bare `static/icons/tofu-welcome.svg`, and the server serves icons with
   `cache-control: public, max-age=86400`. So any change to the file was
   invisible for up to 24h — a logo rollback looked like it "didn't happen",
   and the owner spent a day looking at art that had already been reverted.
   Role icons already solved this with a `?v=` token (orchestration-catalog.js
   `_ORCH_ICON_VER`); the main logo did not. `LOGO_VER` below is that token.
   Bump it whenever the art changes.

   ── ONE MASCOT, NO SWITCHING (owner decision, 2026-07-29) ──────────────
   This module briefly carried a skin registry so candidate mascots could be
   worn at runtime and judged in place. That mechanism is GONE, by explicit
   instruction: "I only want the original version … the others are too ugly
   and there is really no need to switch." It is the THIRD veto of mascot
   switching (the +40% redraw and the A2 soft-edge were each approved from a
   contact sheet and then reversed once live; the project-bar pet's in-bar
   switcher was cut for the same reason).

   So: do NOT reintroduce a skin list, a Settings picker, or a localStorage
   preference here. Judging brand art still requires seeing it in place — but
   the way to do that is to swap the shipped asset behind a bumped LOGO_VER
   on a branch, not to ship a wardrobe to users. tests/test_brand_mascot_single.py
   holds this as a ratchet: the module must resolve exactly one URL.

   Every render site MUST go through brandLogoImgAttrs() (or, for the two
   static tags in index.html, carry the same `?v=` token) — a hand-written
   bare path silently loses the cache-bust, which is the one failure this
   module exists to prevent.

   This file is concatenated by Vite's module graph; symbols share the global
   window scope. No exports.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  /* Bump on every change to the mascot art (defeats the 24h icon cache).
   * index.html's two static tags carry this same token — keep them in sync;
   * tests/test_brand_mascot_single.py asserts they match. */
  var LOGO_VER = '20260729a';

  /* The one shipped mascot. */
  var LOGO_PATH = '/static/icons/tofu-welcome.svg';

  function _base() {
    return (typeof BASE_PATH !== 'undefined' && BASE_PATH) ? BASE_PATH : '';
  }

  /** The brand mascot URL, always cache-busted. */
  function logoUrl() {
    return _base() + LOGO_PATH + '?v=' + LOGO_VER;
  }

  /** The <img> attribute soup every render site needs, as one string.
   *  Callers splice this into their template so the marker attribute and the
   *  cache-busted URL can never drift apart. */
  function brandLogoImgAttrs(size) {
    var s = size || 64;
    return 'src="' + logoUrl() + '" data-brand-logo="1" alt="Tofu" '
      + 'width="' + s + '" height="' + s + '"';
  }

  runtimeScope.LOGO_VER = LOGO_VER;
  runtimeScope.logoUrl = logoUrl;
  runtimeScope.brandLogoImgAttrs = brandLogoImgAttrs;
})();
const brandLogoImgAttrs = runtimeScope.brandLogoImgAttrs;

