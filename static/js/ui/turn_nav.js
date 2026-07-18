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
/* ★ Perf: skip rebuild when user message count + last user content haven't changed.
 * buildTurnNav scans ALL messages and JSON.parse-s tool args (_turnWriteInfo),
 * which costs 50-200ms for large conversations. During streaming, only assistant
 * content changes — user messages are static, so the turn nav doesn't need updates. */
let _turnNavFp = "";
function buildTurnNav(conv) {
  const nav = document.getElementById("turnNav");
  if (!nav) return;
  if (!conv || conv.messages.length === 0) {
    nav.innerHTML = "";
    _turnNavFp = "";
    return;
  }
  /* Fingerprint: count of user messages + last user message content (first 40 chars) */
  let _uCount = 0, _lastUContent = "";
  for (let i = 0; i < conv.messages.length; i++) {
    if (conv.messages[i].role === "user") {
      _uCount++;
      _lastUContent = (conv.messages[i].content || "").slice(0, 40);
    }
  }
  const _fp = _uCount + ":" + _lastUContent + ":" + conv.messages.length;
  if (_fp === _turnNavFp) return;
  _turnNavFp = _fp;
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
function scrollToTurn(idx) {
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
      inner.prepend(frag);
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
      renderChat(conv, true);
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

