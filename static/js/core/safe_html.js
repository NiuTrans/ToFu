/* ═══════════════════════════════════════════════════════════════════
   core/safe_html.js — auto-escaping tagged-template helper

   `safeHtml` is a tagged template that escapes EVERY interpolation by
   default, then returns a plain HTML **string** — so it is a drop-in
   replacement for the existing string-building render code
   (`el.outerHTML = renderMessage(...)`, `insertAdjacentHTML(...)`,
   `html += ...`). It does NOT introduce a build step or a runtime
   framework; it loads after core/escape_html.js and shares window scope.

   Why: the chat-render path builds HTML by string concatenation. Today
   every user/model interpolation is hand-wrapped in escapeHtml(), which
   is correct but fragile — one forgotten wrap is an XSS hole. `safeHtml`
   makes escaping the DEFAULT and intentional-HTML the explicit exception,
   so the safe path is the path of least resistance.

   Usage
   -----
       safeHtml`<div title="${userText}">${userText}</div>`
       // → both interpolations escaped

       // Intentional HTML (already sanitized / trusted) opts out via raw():
       safeHtml`<div class="md-content">${raw(renderMarkdown(msg.content))}</div>`
       safeHtml`<span class="avatar">${raw(_TOFU_WORKER_SVG)}</span>`

       // Arrays are joined (the common `.map().join('')` pattern); each
       // element is escaped (or left raw if it is a raw()/safeHtml result):
       safeHtml`<ul>${items.map(it => safeHtml`<li>${it.name}</li>`)}</ul>`

   Escaping contract
   -----------------
   - Strings / numbers / booleans → escapeHtml(String(value)).
   - null / undefined → '' (so `${maybeNull}` is a no-op, matching the
     `${x || ''}` idiom).
   - raw(x) → x verbatim (caller asserts it is already safe HTML).
   - A value produced by safeHtml itself is already escaped+trusted, so it
     is spliced verbatim (nested templates compose without double-escaping).
   - Arrays → each element run through the same rules, then concatenated.

   IMPORTANT: `raw()` is the ONLY escape hatch. Pass it ONLY values that
   are either (a) the output of renderMarkdown() (DOMPurify-sanitized) or
   (b) a hardcoded constant (SVG icon, static markup) — NEVER raw user or
   model text. The chat-render lint rule (tests/test_frontend_safe_html.py)
   enforces that user/model fields go through safeHtml, not bare template
   strings.
   ═══════════════════════════════════════════════════════════════════ */

/* Brand for values that must bypass escaping. Uses a class so
 * `instanceof` is cheap and can't be spoofed by a plain object coming
 * from JSON (defense against a model echoing `{__safeHtmlRaw:true}`). */
class _SafeHtmlRaw {
  constructor(value) {
    this.value = value == null ? '' : String(value);
  }
}

/** Mark a string as trusted HTML that `safeHtml` must NOT escape.
 *  Pass ONLY DOMPurify-sanitized markdown output or hardcoded markup. */
function raw(value) {
  return new _SafeHtmlRaw(value);
}

/** Coerce one interpolated value to its final HTML string per the
 *  escaping contract above. */
function _safeHtmlPart(value) {
  if (value == null) return '';
  if (value instanceof _SafeHtmlRaw) return value.value;
  if (Array.isArray(value)) {
    let out = '';
    for (let i = 0; i < value.length; i++) out += _safeHtmlPart(value[i]);
    return out;
  }
  // escapeHtml() (core/escape_html.js) handles non-string coercion + null.
  return escapeHtml(typeof value === 'string' ? value : String(value));
}

/**
 * Tagged template that auto-escapes every interpolation and returns the
 * assembled HTML string.
 * @returns {_SafeHtmlRaw} a raw-marked result, so nested `safeHtml`
 *   templates compose without being re-escaped. Call `.value` (or rely on
 *   String coercion) to get the plain string at a top-level sink.
 */
function safeHtml(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    out += _safeHtmlPart(values[i]) + strings[i + 1];
  }
  return new _SafeHtmlRaw(out);
}

/* Make a top-level safeHtml result usable anywhere a string is expected
 * (e.g. `el.outerHTML = safeHtml\`...\``, `insertAdjacentHTML(pos, ...)`).
 * _SafeHtmlRaw.toString() returns the underlying HTML, so the existing
 * string sinks keep working unchanged. */
_SafeHtmlRaw.prototype.toString = function () { return this.value; };
