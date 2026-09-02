/* ===== migrated source: cookie_capture_consent.js ===== */
/* Thin ambient adapter for the typed cookie-capture subscription owner. */
(function () {
  "use strict";

  /** @type {import('../core/cookie-capture-consent').CookieCaptureConsentController|null} */
  let controller = null;
  let destroyed = false;

  /** @returns {import('../core/cookie-capture-consent').CookieCaptureConsentDependencies} */
  function dependencies() {
    return {
      subscribe(channel, taskId, handler) {
        if (typeof pushSubscribe === "function") {
          pushSubscribe(channel, taskId, handler);
        }
      },
      unsubscribe(channel, taskId, handler) {
        if (typeof pushUnsubscribe === "function") {
          pushUnsubscribe(channel, taskId, handler);
        }
      },
      showToast(message, kind) {
        if (typeof showToast === "function") showToast(message, kind);
      },
      translate(key) {
        return typeof t === "function" ? t(key) : key + " {domain}";
      },
    };
  }

  function initCookieCaptureConsent() {
    if (destroyed || controller) return;
    controller = createCookieCaptureConsentController(dependencies());
  }

  function destroyCookieCaptureConsent() {
    if (destroyed) return;
    destroyed = true;
    controller?.destroy();
    controller = null;
  }

  /** @param {unknown} frame */
  function handleCookieCaptureConsentFrame(frame) {
    return handleTypedCookieCaptureFrame(frame, dependencies());
  }

  runtimeScope.CookieCaptureConsent = {
    init: initCookieCaptureConsent,
    destroy: destroyCookieCaptureConsent,
    _handleFrame: handleCookieCaptureConsentFrame,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCookieCaptureConsent, { once: true });
  } else {
    initCookieCaptureConsent();
  }
})();

