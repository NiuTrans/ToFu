/* ═══════════════════════════════════════════════════════════════════
   turn nav — extracted from ui.js (split 2026-05-28)

   Turn navigation: build dots, scroll-to-turn, active-dot tracking.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Turn navigation ──
function _turnWriteInfo(conv, userMsgIdx) {
  /* Scan the assistant response for this turn — collect modified file names.
   * Returns array of short filenames, or null if no writes. */
  const files = new Set();
  for (let j = userMsgIdx + 1; j < conv.messages.length; j++) {
    const m = conv.messages[j];
    if (m.role === 'user') break; // hit the next turn
    for (const r of (m.toolRounds || [])) {
      if ((r.toolName === 'write_file' || r.toolName === 'apply_diff' || r.toolName === 'apply_diffs' || r.toolName === 'insert_content' || r.toolName === 'insert_contents') && r.status === 'done') {
        // toolArgs is a JSON string, not a parsed object
        try {
          const args = typeof r.toolArgs === 'string' ? JSON.parse(r.toolArgs) : (r.toolArgs || {});
          if (args.path) {
            files.add(args.path.split('/').pop());
          } else if (Array.isArray(args.edits)) {
            // apply_diff batch mode: edits[].path
            for (const e of args.edits) {
              if (e?.path) files.add(e.path.split('/').pop());
            }
          }
        } catch (_) {
          // Malformed toolArgs — still mark as write turn even without filename
          files.add('(unknown)');
        }
      }
    }
  }
  return files.size > 0 ? [...files] : null;
}
/* ★ Perf: skip the rebuild when nothing a dot RENDERS FROM has changed.
 * buildTurnNav scans ALL messages and JSON.parse-s tool args (_turnWriteInfo),
 * which costs 50-200ms for large conversations. During streaming only assistant
 * content changes — user messages are static, so the nav needs no update.
 *
 * ★ The fingerprint must sample EVERY input a dot is built from, or the guard
 * strands dots that address messages which have moved. It previously sampled
 * `userCount + LAST user content + length` only, which is blind to two things
 * that DO change the dots:
 *   • WHICH CONVERSATION this is. `_turnNavFp` is one module-level slot shared
 *     across conversations, so switching to a different conv with the same
 *     shape (same length, same user count, same trailing user text) was a HIT →
 *     early return → the sidebar kept the PREVIOUS conversation's dots, whose
 *     `data-msg-idx` / `scrollToTurn(idx)` now index a different array.
 *   • Any NON-LAST user message — edited, deleted mid-history, or shifted by an
 *     insert. Only the tail was sampled, so those rebuilt nothing.
 * Seed with `conv.id` and fold each turn's INDEX + content preview, hashed to
 * keep the token short. Still O(messages) with no JSON.parse, so the streaming
 * skip (the reason this guard exists) is unaffected. */
let _turnNavFp = "";
function buildTurnNav(conv) {
  const nav = document.getElementById("turnNav");
  if (!nav) return;
  if (!conv || conv.messages.length === 0) {
    nav.innerHTML = "";
    _turnNavFp = "";
    return;
  }
  let _fpSeed = conv.id + "|";
  let _uCount = 0;
  for (let i = 0; i < conv.messages.length; i++) {
    const m = conv.messages[i];
    if (m.role !== "user") continue;
    _uCount++;
    _fpSeed += i + "=" + (m._isEndpointReview ? "R" : "") + (m.content || "").slice(0, 40) + ";";
  }
  const _fp = _uCount + ":" + conv.messages.length + ":" +
    (typeof _hashStr === "function" ? _hashStr(_fpSeed) : _fpSeed);
  if (_fp === _turnNavFp) return;
  _turnNavFp = _fp;
  /* A rebuild replaces every dot node, so the cached active-dot index no longer
   * refers to anything. Clear it or updateActiveTurn can compute the same index
   * as before, take its `ai !== _lastActiveDotIdx` early-out, and leave the
   * fresh nav with NO dot marked active. */
  _lastActiveDotIdx = -1;
  let tn = 0;
  const turns = [];
  for (let i = 0; i < conv.messages.length; i++) {
    const role = conv.messages[i].role;
    if (role === "user") {
      tn++;
      const isCritic = !!conv.messages[i]._isEndpointReview;
      const writeFiles = _turnWriteInfo(conv, i);
      const rawPreview = (conv.messages[i].content || "").split("\n")[0].slice(0, 40);
      turns.push({
        num: tn,
        msgIdx: i,
        preview: isCritic ? `${rawPreview}` : rawPreview,
        isCritic: isCritic,
        writeFiles: writeFiles,
      });
    }
  }
  if (turns.length < 2) {
    nav.innerHTML = "";
    return;
  }
  nav.innerHTML =
    '<div class="turn-nav-label">Turns</div>' +
    turns
      .map((t) => {
        const safe = t.preview
          .replace(/&/g, "&amp;")
          .replace(/"/g, "&quot;")
          .replace(/</g, "&lt;");
        const criticCls = t.isCritic ? ' turn-dot-critic' : '';
        const writesCls = t.writeFiles ? ' turn-dot-writes' : '';
        const writeTip = t.writeFiles ? (' ' + t.writeFiles.join(', ')).replace(/"/g, '&quot;') : '';
        return `<div class="turn-dot${criticCls}${writesCls}" data-msg-idx="${t.msgIdx}" onclick="scrollToTurn(${t.msgIdx})" title="Turn ${t.num}: ${safe}${writeTip}">${t.num}</div>`;
      })
      .join("");
  requestAnimationFrame(() => updateActiveTurn());
  /* Dev-only sanity check: ensure every turn-dot points at a msg in the DOM
   * (scrollToTurn has a re-render fallback anyway, but this surfaces the
   * regression early during development). Enable with window._TOFU_DEV_ASSERT=1 */
  if (typeof window !== 'undefined' && window._TOFU_DEV_ASSERT) {
    requestAnimationFrame(() => {
      for (const t of turns) {
        if (!document.getElementById('msg-' + t.msgIdx)) {
          console.warn('[turnNav] missing msg-%d (turn %d)', t.msgIdx, t.num);
        }
      }
    });
  }
}
function scrollToTurn(idx, _noRerender) {
  let el = document.getElementById("msg-" + idx);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const conv = conversations.find((c) => c.id === _lazyConvId);
  if (!conv) return;
  const inner = document.getElementById("chatInner");
  const container = document.getElementById("chatContainer");
  if (!inner || !container) return;

  /* Case A0: the target was evicted BELOW the rendered window.
   *
   * The bounded render window (_MAX_RENDER_WINDOW, streaming_render.js) drops
   * TAIL bubbles when the reader scrolls up through history, so the rendered
   * span becomes e.g. [160,240) of 300 messages. A dot for index 250 then has
   * no node, and this case was MISSING — the only window walk here was the
   * upward one (Case A). Falling through to the force-re-render below does NOT
   * rescue it: that repaints only the tail window [total-_INITIAL_RENDER,
   * total), so any target above that stayed absent and the click was a SILENT
   * no-op (the reported "some dots are unresponsive").
   *
   * Walk the EXISTING downward loader instead of hand-rolling a second
   * renderer: `_loadNewerMessages` already owns the bottom sentinel, the head
   * eviction that keeps the window bounded, and the scroll compensation. */
  if (Number.isFinite(_lazyRenderedTo) && idx >= _lazyRenderedTo) {
    /* Bound the walk by the number of BATCHes that could possibly be needed,
     * and break the moment a call makes no progress (e.g. a live stream owns
     * the tail, so _loadNewerMessages declines) — never spin. */
    let guard = Math.ceil(conv.messages.length / 20) + 2;
    while (guard-- > 0 && Number.isFinite(_lazyRenderedTo) && idx >= _lazyRenderedTo) {
      const before = _lazyRenderedTo;
      _loadingNewer = false;
      _loadNewerMessages();
      if (_lazyRenderedTo === before) break;
    }
    el = document.getElementById("msg-" + idx);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    /* Still missing — fall through to the re-render fallback below. */
  }

  /* Case A: target idx is above the currently rendered range — lazy-load upward */
  if (idx < _lazyRenderedFrom) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    const targetStart = Math.max(0, idx);
    const endIdx = _lazyRenderedFrom;
    let html = "";
    for (let i = targetStart; i < endIdx; i++) {
      html += renderMessage(conv.messages[i], i);
    }

    const prevScrollTop = container.scrollTop;
    const prevScrollHeight = container.scrollHeight;

    const wrapper = document.createElement("div");
    wrapper.innerHTML = html;
    const frag = document.createDocumentFragment();
    while (wrapper.firstChild) frag.appendChild(wrapper.firstChild);
    if (sentinel) {
      sentinel.after(frag);
    } else {
      /* No head sentinel: insert at the HEAD via the shared furniture-aware
       * primitive. A raw `inner.prepend` would land ABOVE a bottom sentinel's
       * sibling set correctly today, but it also ignores any leading
       * furniture — the same class of bug as the head/tail anchors. */
      if (typeof chatInnerInsert === 'function') {
        chatInnerInsert(inner, frag, { position: 'head', conv: conv, site: 'turn_nav.scrollToTurn' });
      } else {
        inner.prepend(frag);
      }
    }
    _lazyRenderedFrom = targetStart;

    /* Fix scroll position so current view doesn't jump */
    container.scrollTop = prevScrollTop + (container.scrollHeight - prevScrollHeight);

    /* Update or remove sentinel */
    if (sentinel) {
      if (targetStart <= 0) {
        sentinel.remove();
      } else {
        const countEl = sentinel.querySelector("._lazy-count");
        if (countEl) countEl.textContent = targetStart;
        if (_lazyObserver) _lazyObserver.observe(sentinel);
      }
    }

    /* Now scroll to the newly rendered element */
    el = document.getElementById("msg-" + idx);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }

  /* Case B: idx >= _lazyRenderedFrom but element still missing from DOM.
   * Known cause: showStreamingUIForConv() used to slice(0,-1) the messages
   * array unconditionally, so a trailing user/critic-done message (for which
   * buildTurnNav DOES produce a dot) was never rendered as msg-{idx}.
   * Defense-in-depth: force a re-render so the missing message appears. */
  if (idx >= 0 && idx < conv.messages.length) {
    console.warn("[scrollToTurn] msg-%d missing but idx>=_lazyRenderedFrom=%d — forcing re-render", idx, _lazyRenderedFrom);
    if (activeStreams.has(conv.id) && typeof showStreamingUIForConv === 'function') {
      showStreamingUIForConv(conv.id);
    } else {
      window.ConvView.replaceAll(conv.id, { forceScroll: true });
    }
    el = document.getElementById("msg-" + idx);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      /* Still not in DOM — it must be rendered as the streaming bubble. */
      const sm = document.getElementById("streaming-msg");
      if (sm) sm.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
}
/* ★ Perf: (1) cache one getBoundingClientRect for container, (2) only touch classList
 * when active dot actually changes, (3) walk incrementally from the previous
 * active dot instead of rescanning from 0. */
let _lastActiveDotIdx = -1;
function updateActiveTurn() {
  const nav = document.getElementById("turnNav");
  if (!nav || !nav.children.length) return;
  const ct = _getChatContainer() || document.getElementById("chatContainer");
  if (!ct) return;
  const ctRect = ct.getBoundingClientRect();
  const thr = ctRect.top + ctRect.height * 0.3;
  const dots = nav.querySelectorAll(".turn-dot");
  const n = dots.length;
  if (!n) return;
  /* Top of a dot's message element, or null when it's lazy-unrendered (an
   * older message scrolled out of the rendered window — skip, don't break). */
  const topOf = (i) => {
    const el = document.getElementById("msg-" + dots[i].getAttribute("data-msg-idx"));
    return el ? el.getBoundingClientRect().top : null;
  };
  /* The active dot is the highest-index RENDERED dot whose top is at/above the
   * threshold line. Dot tops increase monotonically with index, so a linear
   * scan from 0 costs O(activeIdx) getBoundingClientRect reads per frame —
   * O(N) when reading near the bottom of a long conversation, which is the
   * dominant scroll-jank cost. Scrolling moves the boundary by ~1 dot per
   * frame, so seed from the previous active index and walk only the needed
   * direction (O(delta)). The result is identical to the full scan. */
  const prev = (_lastActiveDotIdx >= 0 && _lastActiveDotIdx < n) ? _lastActiveDotIdx : -1;
  const tp = prev >= 0 ? topOf(prev) : null;
  let ai;
  if (prev < 0 || tp === null) {
    /* No usable anchor (first run, nav rebuilt, or the anchored dot was
     * lazy-unrendered) — full forward scan with early break. Rare; not the
     * hot scroll path. */
    ai = 0;
    for (let i = 0; i < n; i++) {
      const t = topOf(i);
      if (t === null) continue;
      if (t <= thr) ai = i;
      else break;
    }
  } else if (tp <= thr) {
    /* Anchor is at/above the line — every earlier dot is too, so climb forward
     * while later rendered dots stay at/above the line. */
    ai = prev;
    for (let i = prev + 1; i < n; i++) {
      const t = topOf(i);
      if (t === null) continue;
      if (t <= thr) ai = i;
      else break;
    }
  } else {
    /* Anchor is below the line — every later dot is too, so descend to the
     * highest rendered dot at/above the line (0 if none qualifies). */
    ai = 0;
    for (let i = prev - 1; i >= 0; i--) {
      const t = topOf(i);
      if (t === null) continue;
      if (t <= thr) { ai = i; break; }
    }
  }
  if (ai !== _lastActiveDotIdx) {
    if (_lastActiveDotIdx >= 0 && _lastActiveDotIdx < dots.length)
      dots[_lastActiveDotIdx].classList.remove("active");
    dots[ai].classList.add("active");
    _lastActiveDotIdx = ai;
  }
}

