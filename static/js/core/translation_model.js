/* ═══════════════════════════════════════════════════════════════════
   core/translation_model.js — the canonical translation data model

   PURPOSE (decoupling steps 1–4, strangler-fig)
   ---------------------------------------------
   Automatic translation and content DISPLAY are being separated into two
   independent modules. Historically the display state was smeared across ~11
   loose per-message fields plus a per-segment field, and both the render path
   AND the translation engine had to know the whole state machine (spinners,
   partials, errors, the 原文/译文 toggle) plus the "inversion" (normal user vs
   VU/critic bilingual wiring). This module is the single seam:

       msg.translation (canonical, via readTranslation) = {
         status:   'idle' | 'pending' | 'done' | 'error',
         done,      // raw _translateDone tri-state (undefined|false|true), verbatim
         text,      // the 译文 (was msg.translatedContent)
         cache,     // backward-compat mirror (was msg._translatedCache)
         showing,   // 原文/译文 toggle (was msg._showingTranslation; tri-state)
         model,     // was msg._translateModel
         error,     // was msg._translateError
         taskId,    // was msg._translateTaskId
         statusMsg, // was msg._translateStatus
         statusKind,// was msg._translateStatusKind
         partial,   // streaming preview (was msg._translatePartial)
         segByRound,// {round: 中文} pending per-segment map (_pendingSegTranslations)
       }

   plus a PURE content resolver `displayContent(msg)` that decides WHICH content
   string to show and how, with ZERO translation logic (erasing the render-path
   inversion), and `translationFingerprint(msg)` for the surgical-diff cache.

   MIGRATION SAFETY — the two directions ship together during the strangler-fig:
     • readTranslation(msg)         legacy fields → canonical object.
     • projectTranslation(msg, tr)  canonical object → legacy fields.
   projectTranslation is a straight FIELD MIRROR: it clears the owned legacy
   keys, then re-writes each one iff its canonical counterpart is present, and
   reproduces `_translateDone` verbatim from `tr.done`. Therefore

       projectTranslation(m, readTranslation(m)) ≡ m

   byte-identically for EVERY message shape — the guarantee the golden test
   locks down, and what lets later increments delete legacy readers safely.

   NOTE: the user-edit `field:'content'` translate path rewrites msg.content /
   msg.originalContent — that is a CONTENT mutation, not display-translation, so
   it is deliberately OUTSIDE this model (displayContent resolves it). The
   send-path `_translateFailed` flag (a user CN→EN send failed) is likewise a
   send-path concern, owned by translation_indicator.js, not this model.

   Bundled by lib/js_bundler.py — pure, no DOM. Loaded after core/conversations
   and core/translate_guard, before translation.js / ui consumers.
   ═══════════════════════════════════════════════════════════════════ */

/* The legacy per-message translation keys this model owns and round-trips.
 * Enumerated once so projectTranslation can clear them deterministically before
 * re-deriving. (_pendingSegTranslations is handled separately — see below.) */
const _LEGACY_TR_KEYS = [
  'translatedContent', '_translatedCache', '_showingTranslation',
  '_translateModel', '_translateDone', '_translateError',
  '_translateTaskId', '_translateStatus', '_translateStatusKind',
  '_translatePartial', '_translateFailed',
];

/**
 * Derive the canonical translation object from a message's legacy fields.
 * Only present keys are copied, so projectTranslation can reproduce absence
 * exactly. `done` carries the raw `_translateDone` tri-state verbatim.
 *
 * @param {object} msg
 * @returns {object} canonical translation (never null)
 */
function readTranslation(msg) {
  const tr = {};
  if (!msg || typeof msg !== 'object') { tr.status = 'idle'; return tr; }

  const hasDone = Object.prototype.hasOwnProperty.call(msg, '_translateDone');
  if (hasDone) tr.done = msg._translateDone;

  // status — semantic convenience for consumers.
  //   error wins; then a resolved done:true; then any pending signal
  //   (_translateDone===false is the server RUNNING-frame marker, so it counts
  //   as pending even with no task/partial/status yet); else idle.
  if (msg._translateError) tr.status = 'error';
  else if (tr.done === true) tr.status = 'done';
  else if (tr.done === false || msg._translateTaskId || msg._translatePartial || msg._translateStatus)
    tr.status = 'pending';
  else tr.status = 'idle';

  // payload / metadata — each mirrored 1:1, present-only.
  if (msg.translatedContent != null) tr.text = msg.translatedContent;
  if (msg._translatedCache != null) tr.cache = msg._translatedCache;
  if (Object.prototype.hasOwnProperty.call(msg, '_showingTranslation'))
    tr.showing = msg._showingTranslation;
  if (msg._translateModel != null) tr.model = msg._translateModel;
  if (msg._translateError != null) tr.error = msg._translateError;
  if (msg._translateTaskId != null) tr.taskId = msg._translateTaskId;
  if (msg._translateStatus != null) tr.statusMsg = msg._translateStatus;
  if (msg._translateStatusKind != null) tr.statusKind = msg._translateStatusKind;
  if (msg._translatePartial != null) tr.partial = msg._translatePartial;
  // Send-path CN→EN failure marker (a user turn whose auto-translate before
  // send failed → original was sent). Distinct concern from display-translation
  // but carried here so chat_render reads NO _translate* field directly.
  if (msg._translateFailed != null) tr.sendFailed = msg._translateFailed;
  if (msg._pendingSegTranslations != null) tr.segByRound = msg._pendingSegTranslations;
  return tr;
}

/**
 * Write a canonical translation object back onto a message as the exact legacy
 * fields the engine would have produced. A straight field mirror: clear the
 * owned keys, then re-write each iff its canonical counterpart is present. The
 * byte-exact inverse of readTranslation for every message shape.
 *
 * @param {object} msg  mutated in place
 * @param {object} tr   canonical translation (from readTranslation or built)
 * @returns {object} msg
 */
function projectTranslation(msg, tr) {
  if (!msg || typeof msg !== 'object') return msg;
  for (const k of _LEGACY_TR_KEYS) delete msg[k];
  if (!tr) return msg;
  if (tr.done !== undefined) msg._translateDone = tr.done;
  if (tr.text != null) msg.translatedContent = tr.text;
  if (tr.cache != null) msg._translatedCache = tr.cache;
  if (tr.showing !== undefined) msg._showingTranslation = tr.showing;
  if (tr.model != null) msg._translateModel = tr.model;
  if (tr.error != null) msg._translateError = tr.error;
  if (tr.taskId != null) msg._translateTaskId = tr.taskId;
  if (tr.statusMsg != null) msg._translateStatus = tr.statusMsg;
  if (tr.statusKind != null) msg._translateStatusKind = tr.statusKind;
  if (tr.partial != null) msg._translatePartial = tr.partial;
  if (tr.sendFailed != null) msg._translateFailed = tr.sendFailed;
  return msg;
}

/**
 * PURE content-origin resolver: decides WHICH content string a message shows
 * and how to render it, with NO translation state read. This is the erasure of
 * the render-path "inversion" — a normal user message shows its 源文
 * (originalContent), a VU/critic message shows its markdown content, an
 * assistant shows its markdown content. Whether a 译文 is shown instead is a
 * SEPARATE decision (see readTranslation.status) that the render layer composes
 * on top — not this function's job.
 *
 * @param {object} msg
 * @returns {{text:string, isMarkdown:boolean, stripNoTranslate:boolean}}
 */
function displayContent(msg) {
  if (!msg || typeof msg !== 'object') return { text: '', isMarkdown: false, stripNoTranslate: false };
  // Must mirror chat_render.js `isUser` EXACTLY (optimizer = endpoint review,
  // rendered as user) so this resolver is byte-identical to the render it replaces.
  const isUser = msg.role === 'user' || msg.role === 'optimizer';
  const isCritic = isUser && (msg._isEndpointReview || msg._isVirtualUser);
  if (isCritic) return { text: msg.content || '', isMarkdown: true, stripNoTranslate: false };
  if (isUser) return { text: (msg.originalContent || msg.content) || '', isMarkdown: false, stripNoTranslate: true };
  return { text: msg.content || '', isMarkdown: true, stripNoTranslate: false };
}

/**
 * Compact translation-state contribution to a message's surgical-diff
 * fingerprint. Byte-identical to the inline expression chat_render.js used
 * (translatedContent length : showing T/F : pending P) so routing it through
 * the model does not change when the diff repaints.
 *
 * @param {object} msg
 * @returns {string}
 */
function translationFingerprint(msg) {
  const tr = readTranslation(msg);
  return (tr.text != null ? String(tr.text).length : 0) + ':' +
         (tr.showing ? 'T' : 'F') + ':' +
         (tr.done === false ? 'P' : '');
}

/**
 * PURE stale-partial detector: a translation produced from mid-stream partial
 * content tends to be a small fraction of the (now-final) source. This is the
 * synchronous policy shared by needsTranslation and the pipeline's re-translate
 * guard. Thresholds are backend-owned (lib/text_lang.is_stale_partial_translation,
 * served on window._translationPolicy); `policy` may be passed explicitly for
 * a pure call, else it falls back to the window policy then the constants.
 *
 * @param {object} msg
 * @param {{stale_frac?:number, min_source_chars?:number}} [policy]
 * @returns {boolean}
 */
function isStalePartialTranslation(msg, policy) {
  const tr = readTranslation(msg);
  if (tr.text == null || !msg || !msg.content) return false;
  const p = policy || (typeof window !== 'undefined' && window._translationPolicy) || null;
  const frac = (p && p.stale_frac > 0) ? p.stale_frac : 0.15;
  const minSrc = (p && p.min_source_chars > 0) ? p.min_source_chars : 500;
  return msg.content.length > minSrc &&
         String(tr.text).length < msg.content.length * frac;
}

/**
 * THE SINGLE "should this message be auto-translated?" DECISION.
 *
 * Historically this question was smeared across three places with divergent
 * answers: the backend net (`resolve_auto_translate` on the send-time-FROZEN
 * per-conv flag), the frontend resume (Phase 0b, last-assistant-only + break),
 * and the effective-toggle resolver. This pure predicate is the one authority
 * every trigger path consults — mirroring how displayContent/readTranslation
 * unified the render decision.
 *
 * It composes ONLY the SYNCHRONOUS gates (so it stays pure + testable):
 *   • auto-translate must be effectively ON (pass the resolved boolean in
 *     `opts.autoTranslateOn` — callers resolve it via convAutoTranslateEffective;
 *     the async network language check is NOT here, it stays in the pipeline).
 *   • the message must be a DISPLAY-translated role (assistant, or a
 *     critic/VU user — same set displayContent marks isMarkdown) with content.
 *   • image-gen results (_igResult/_igResults/_isImageGen) are never translated.
 *   • already-translated messages are skipped UNLESS the translation is a stale
 *     mid-stream partial (then it needs re-translating).
 *   • a message already terminal/in-flight (done true, or a live task, or a
 *     server RUNNING frame) is left to its owner — not re-dispatched — EXCEPT
 *     the stale-partial case above.
 *
 * @param {object} msg
 * @param {object} conv  (unused today; kept so callers pass context and the
 *                        signature is stable if per-conv policy is added)
 * @param {{autoTranslateOn:boolean, policy?:object}} opts
 * @returns {boolean}
 */
function needsTranslation(msg, conv, opts) {
  opts = opts || {};
  if (!opts.autoTranslateOn) return false;
  if (!msg || typeof msg !== 'object') return false;
  // Image-gen bubbles have no translatable prose.
  if (msg._igResult || msg._igResults || msg._isImageGen) return false;
  // Only DISPLAY-translated roles: assistant, or critic/VU users (the exact
  // set displayContent renders as markdown — a plain user shows 源文, never a
  // 译文, so it is never auto-translated to Chinese here).
  const isUser = msg.role === 'user' || msg.role === 'optimizer';
  const isCritic = isUser && (msg._isEndpointReview || msg._isVirtualUser);
  const isDisplayTranslated = (msg.role === 'assistant') || isCritic;
  if (!isDisplayTranslated) return false;
  if (!msg.content || !String(msg.content).trim()) return false;

  const tr = readTranslation(msg);
  // A stale mid-stream partial is the ONE already-touched state that still
  // needs (re)translation.
  if (isStalePartialTranslation(msg, opts.policy)) return true;
  // Otherwise: anything already resolved / owned by a live task / mid-flight
  // server frame is left to its owner.
  if (tr.text != null) return false;              // already has a 译文
  if (tr.done === true) return false;             // terminally done (e.g. skipped)
  if (msg._translateTaskId) return false;         // a client poll loop owns it
  if (tr.done === false) return false;            // server RUNNING frame owns it
  return true;
}

if (typeof window !== 'undefined') {
  window.readTranslation = readTranslation;
  window.projectTranslation = projectTranslation;
  window.displayContent = displayContent;
  window.translationFingerprint = translationFingerprint;
  window.isStalePartialTranslation = isStalePartialTranslation;
  window.needsTranslation = needsTranslation;
}
