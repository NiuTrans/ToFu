/* tofu-auto-render.js — Minimal KaTeX auto-render for sandboxed artifact iframes.
 *
 * Loaded inside /api/artifacts/<id>/view together with katex.min.js.  Walks
 * text nodes and replaces $...$ / $$...$$ / \(...\) / \[...\] with
 * KaTeX-rendered HTML.  Skips <script>, <style>, <pre>, <code>, <textarea>,
 * and any node already inside a `.katex` element.
 *
 * Why hand-rolled instead of katex/contrib/auto-render?
 *   The official auto-render isn't shipped with our static/vendor/katex/
 *   bundle, and pulling in the full ~25 KB contrib script for what is
 *   ~80 lines is unnecessary.  This file is loaded with a `defer` from
 *   `script-src 'self'`, so it does NOT need 'unsafe-inline' (which we
 *   deliberately do not grant — it would let model <script> blocks run).
 */
(function () {
  "use strict";

  var DELIMS = [
    { left: "$$",  right: "$$",  display: true  },
    { left: "\\[", right: "\\]", display: true  },
    { left: "\\(", right: "\\)", display: false },
    { left: "$",   right: "$",   display: false },
  ];

  var EXCLUDE_TAGS = {
    SCRIPT:1, STYLE:1, PRE:1, CODE:1, TEXTAREA:1, NOSCRIPT:1, IFRAME:1,
  };

  function whenReady(cb) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", cb, { once: true });
    } else {
      cb();
    }
  }

  function whenKatex(cb) {
    if (typeof katex !== "undefined") { cb(); return; }
    var tries = 0;
    var t = setInterval(function () {
      if (typeof katex !== "undefined") {
        clearInterval(t);
        cb();
      } else if (++tries > 200) {  // give up after 10 s
        clearInterval(t);
      }
    }, 50);
  }

  function isEscaped(text, pos) {
    if (pos === 0) return false;
    var slashes = 0;
    var i = pos - 1;
    while (i >= 0 && text.charAt(i) === "\\") { slashes++; i--; }
    return (slashes % 2) === 1;
  }

  function findDelim(text, delim, from) {
    var i = from;
    while (true) {
      var p = text.indexOf(delim, i);
      if (p === -1) return -1;
      if (isEscaped(text, p)) { i = p + 1; continue; }
      // Single-$ heuristic: skip when adjacent to a digit on both sides
      // (e.g. "He paid $50 for $100 of stuff." — no math).  The pair
      // detection in renderText then re-validates the closing.
      if (delim === "$") {
        var prev = p > 0 ? text.charAt(p - 1) : "";
        var next = p + 1 < text.length ? text.charAt(p + 1) : "";
        if (/\d/.test(prev) && /\d/.test(next)) { i = p + 1; continue; }
      }
      return p;
    }
  }

  function renderText(textNode) {
    var text = textNode.nodeValue;
    if (!text) return;
    if (text.indexOf("$") === -1 && text.indexOf("\\(") === -1 && text.indexOf("\\[") === -1) return;

    var parts = [];
    var idx = 0;
    while (idx < text.length) {
      var bestPos = -1, bestDelim = null;
      for (var i = 0; i < DELIMS.length; i++) {
        var p = findDelim(text, DELIMS[i].left, idx);
        if (p !== -1 && (bestPos === -1 || p < bestPos ||
                         (p === bestPos && DELIMS[i].left.length > bestDelim.left.length))) {
          bestPos = p;
          bestDelim = DELIMS[i];
        }
      }
      if (bestPos === -1) {
        parts.push(text.slice(idx));
        break;
      }
      var startMath = bestPos + bestDelim.left.length;
      var closePos = findDelim(text, bestDelim.right, startMath);
      if (closePos === -1 || closePos === startMath) {
        parts.push(text.slice(idx));
        break;
      }
      if (bestPos > idx) parts.push(text.slice(idx, bestPos));
      parts.push({ tex: text.slice(startMath, closePos), display: bestDelim.display });
      idx = closePos + bestDelim.right.length;
    }

    if (parts.length === 1 && typeof parts[0] === "string") return;

    var parent = textNode.parentNode;
    if (!parent) return;
    var frag = document.createDocumentFragment();
    for (var j = 0; j < parts.length; j++) {
      var p2 = parts[j];
      if (typeof p2 === "string") {
        if (p2.length) frag.appendChild(document.createTextNode(p2));
      } else {
        var span = document.createElement("span");
        try {
          span.innerHTML = katex.renderToString(p2.tex, {
            displayMode: p2.display,
            throwOnError: false,
            strict: false,
            trust: false,
          });
        } catch (e) {
          // Fallback to literal; never inject the raw tex into innerHTML.
          var lit = (p2.display ? "$$" : "$") + p2.tex + (p2.display ? "$$" : "$");
          span.textContent = lit;
        }
        frag.appendChild(span);
      }
    }
    parent.replaceChild(frag, textNode);
  }

  function render(root) {
    if (!root) return;
    // Iterative BFS — avoids deep-recursion stack overflow on giant artifacts.
    var queue = [root];
    while (queue.length) {
      var node = queue.shift();
      var c = node.firstChild;
      while (c) {
        var next = c.nextSibling;
        if (c.nodeType === 3 /* TEXT */) {
          renderText(c);
        } else if (c.nodeType === 1 /* ELEMENT */) {
          var tag = c.tagName;
          if (!EXCLUDE_TAGS[tag] &&
              !(c.classList && c.classList.contains("katex"))) {
            queue.push(c);
          }
        }
        c = next;
      }
    }
  }

  whenReady(function () {
    whenKatex(function () {
      try {
        render(document.body);
      } catch (e) {
        try { console.error("[tofu-auto-render]", e); } catch (_) {}
      }
    });
  });
})();
