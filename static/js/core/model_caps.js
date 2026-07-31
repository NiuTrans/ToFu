/* ═══════════════════════════════════════════════════════════════════
   core/model_caps.js — capability taxonomy consumer

   Frontend counterpart of lib/model_info/capability_taxonomy.py. Owns the
   single answer to "is this model a chat model?" for every UI picker.

   Boot order: this file loads early in the CORE bundle so every downstream
   filter (main_toolbar_ui / paper/report / settings/visibility_defaults /
   settings/template_actions) can call window.isChatModel(m).

   At boot the module holds a hardcoded fallback set (the frontend-only
   "chat picker" set — NOT the dispatcher's issubset set; those two differ
   by {'audio_chat'} and that difference is intentional — see
   lib/model_info/capability_taxonomy.py's module docstring).

   Once /api/server-config returns, main_toolbar_ui.js pipes its
   capability_taxonomy field through applyCapabilityTaxonomy(payload) so a
   deployment that adds a NEW non-chat cap (e.g. 'tts', 'video_gen') gets
   correct filtering without a client rebuild.

   This file is concatenated by lib/js_bundler.py; symbols share the global
   window scope. No exports.
   ═══════════════════════════════════════════════════════════════════ */

(function() {
  // Hardcoded fallback — MUST match lib.model_info.capability_taxonomy
  // CHAT_EXCLUDED_CAPS exactly. The Python-side parity test
  // (test_capability_taxonomy_parity.py) enforces byte-equivalence.
  var _FALLBACK_CHAT_EXCLUDED_CAPS = ['image_gen', 'embedding', 'transcription', 'tts'];

  // The live set (may be overwritten by the server payload).
  var _chatExcludedCaps = new Set(_FALLBACK_CHAT_EXCLUDED_CAPS);

  // The dispatcher-shaped set — exposed for debugging / parity harnesses.
  // The frontend never filters with this; it's the backend's ``issubset``
  // set (frontend uses _chatExcludedCaps which excludes 'audio_chat').
  var _dispatcherNonChatCaps = new Set(_FALLBACK_CHAT_EXCLUDED_CAPS.concat(['audio_chat']));

  /** Return true if a model belongs in a chat picker.
   *
   * A caps list that's empty or missing counts as chat (matches the legacy
   * ``caps || ['text']`` default the old inline filters used). Any cap in
   * the exclusion set means "hide from chat picker".
   */
  function isChatModel(m) {
    if (!m) return true;
    var caps = m.capabilities;
    if (!caps || caps.length === 0) return true;
    for (var i = 0; i < caps.length; i++) {
      if (_chatExcludedCaps.has(caps[i])) return false;
    }
    return true;
  }

  /** Ingest the taxonomy payload from /api/server-config or /api/v1/capabilities.
   *
   * Best-effort: if the payload is missing / malformed the module keeps its
   * hardcoded fallback and logs a warning. Ignores the dispatcher-shaped
   * set for filtering purposes — the frontend filter is strictly
   * chat_excluded_caps (frontend semantics; audio_chat is legit chat).
   */
  function applyCapabilityTaxonomy(payload) {
    if (!payload || typeof payload !== 'object') return;
    var xs = payload.chat_excluded_caps;
    if (Array.isArray(xs) && xs.length > 0) {
      _chatExcludedCaps = new Set(xs);
    }
    var ds = payload.dispatcher_non_chat_caps;
    if (Array.isArray(ds) && ds.length > 0) {
      _dispatcherNonChatCaps = new Set(ds);
    }
  }

  /** Return the current chat-excluded set (for debug / tests). */
  function getChatExcludedCaps() {
    return new Set(_chatExcludedCaps);
  }

  window.isChatModel = isChatModel;
  window.applyCapabilityTaxonomy = applyCapabilityTaxonomy;
  window.getChatExcludedCaps = getChatExcludedCaps;
  // Exposed as arrays so tests / debug consoles can read them without going
  // through Set() serialization.
  window.CHAT_EXCLUDED_CAPS_FALLBACK = _FALLBACK_CHAT_EXCLUDED_CAPS.slice();
})();
