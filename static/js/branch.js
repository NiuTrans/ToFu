/* ═══════════════════════════════════════════
   branch.js — Branch Conversations
   ═══════════════════════════════════════════ */

// ══════════════════════════════════════════
// Branch Conversations v4
//   - Map-based streaming: multiple branches can stream in parallel
//   - Streams continue in background when user switches conversations
//   - Full SSE event pipeline: delta, state, phase, tool_start, tool_result, done
//   - Periodic saves during stream (every 3s like main chat)
//   - Multiple reply quotes support
//   - Anchored branches inline in content
// ══════════════════════════════════════════

/* ── Module-level state ── */
let _activeBranch = null;   // { msgIdx, branchIdx } — which panel is open in the UI
// Map: "convId:msgIdx:branchIdx" → { controller, taskId, convId, msgIdx, branchIdx }
const _branchStreams = new Map();

function _branchKey(convId, mi, bi) { return `${convId}:${mi}:${bi}`; }

function _isBranchStreaming(convId, mi, bi) {
  return _branchStreams.has(_branchKey(convId, mi, bi));
}

// Check if a taskId is managed by a branch stream (used by initActiveTasks to avoid orphan collision)
function isBranchTaskId(taskId) {
  for (const s of _branchStreams.values()) {
    if (s.taskId === taskId) return true;
  }
  // Also check branch.activeTaskId in all conversations
  for (const conv of conversations) {
    for (const msg of (conv.messages || [])) {
      for (const b of (msg.branches || [])) {
        if (b.activeTaskId === taskId) return true;
      }
    }
  }
  return false;
}

// ── Auto-icon based on branch title ──
// Branch creation is now server-authoritative.
// Server endpoint: POST /api/v1/conversations/{id}/messages/{i}/branches
// generates the branch ID, classifies the title (icon + kind), validates
// the message index, persists, and returns the new branch dict + index.
// The JS no longer mints local IDs (race-free) and no longer holds
// classification policy.
async function _createBranchOnServer(convId, msgIdx, title, anchorText, parentSelection) {
  if (!convId || !title) return null;
  try {
    // ★ Routed through the unified API client (Api.conversations.createBranch)
    //   per CLAUDE.md §3.2.0 — no JS file other than api.js may raw-fetch /api/*.
    //   (The old raw `fetch(url, …)` used a variable URL, which the isolation
    //   ratchet's inline-string regex couldn't even see — a silent violation.)
    // ★ Send the anchor's stable _msgId so the server resolves the CURRENT
    //   absolute index — drift-proof under windowed reads where msgIdx is only
    //   a tail-window position, not the absolute index.
    let _anchorMsgId = null;
    try {
      const _c = (typeof conversations !== 'undefined')
        ? conversations.find((c) => c.id === convId) : null;
      _anchorMsgId = _c && _c.messages && _c.messages[msgIdx]
        ? _c.messages[msgIdx]._msgId : null;
    } catch (_e) { /* best-effort */ }
    const r = await Api.conversations.createBranch(convId, msgIdx, {
      title,
      anchor_text: anchorText || '',
      parent_selection: parentSelection || '',
      msg_id: _anchorMsgId || undefined,
    });
    if (!r || !r.ok) {
      if (typeof debugLog === 'function') {
        debugLog(`[Branch] create failed: HTTP ${r ? r.status : 'network error'}`, 'warn');
      }
      return null;
    }
    const body = await r.json();
    if (!body.ok || !body.branch) return null;
    return { branch: body.branch, branchIdx: body.branch_idx };
  } catch (err) {
    if (typeof debugLog === 'function') {
      debugLog(`[Branch] create error: ${err && err.message}`, 'warn');
    }
    return null;
  }
}

// ══════════════════════════════════════════
//  Inject anchored branch pills into rendered markdown
// ══════════════════════════════════════════
// Returns { html, inlinedSet } — inlinedSet is a Set<number> of branch indices that were inlined
function _injectAnchoredBranches(html, msg, msgIdx) {
  const branches = msg.branches || [];
  const inlinedSet = new Set();
  if (!branches.length) return { html, inlinedSet };

  const anchored = [];
  branches.forEach((b, bi) => {
    if (b.anchorText) anchored.push({ b, bi });
  });
  if (!anchored.length) return { html, inlinedSet };

  let out = html;

  // ── Pre-build a plain-text ↔ HTML-index map ONCE (O(n)) ──
  // plainChars[i] = { char, htmlIdx } for each visible character
  function _buildTextMap(src) {
    const map = []; // map[plainIdx] = htmlIdx of that char
    let inTag = false;
    for (let hi = 0; hi < src.length; hi++) {
      if (src[hi] === '<') { inTag = true; continue; }
      if (src[hi] === '>') { inTag = false; continue; }
      if (inTag) continue;
      map.push(hi); // map[map.length-1] = html index of this plain char
    }
    return map;
  }

  for (const { b, bi } of anchored) {
    const anchorPlain = b.anchorText.slice(0, 60).replace(/\s+/g, " ").trim();
    if (!anchorPlain) continue;

    // Build text map for current `out` (rebuilt each iteration since out changes)
    const textMap = _buildTextMap(out);

    // Extract plain text from the map and find anchor position — O(n)
    const plainText = textMap.map(hi => out[hi]).join("").toLowerCase();
    const anchorLower = anchorPlain.toLowerCase();
    const plainStart = plainText.indexOf(anchorLower);
    if (plainStart < 0) continue;

    // Map plain-text start position back to HTML index
    const anchorHtmlStart = textMap[plainStart];
    // Map plain-text end position back to HTML index (one past the last matched char)
    const plainEnd = plainStart + anchorLower.length;
    // walkIdx = just past the last anchor char in HTML
    let walkIdx = (plainEnd < textMap.length) ? textMap[plainEnd] : out.length;

    // If the next char is inside a tag, skip to after it
    let bestInsertPos = walkIdx;
    if (bestInsertPos < out.length && out[bestInsertPos] === '<') {
      const tagEnd = out.indexOf('>', bestInsertPos);
      if (tagEnd >= 0) bestInsertPos = tagEnd + 1;
    }

    const conv = getActiveConv();
    const isActive = _activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === bi;
    const isStreaming = conv && _isBranchStreaming(conv.id, msgIdx, bi);
    const icon = b.icon || '';
    const count = (b.messages || []).filter(m => m.role === "user").length;

    let pillHtml = `<div class="branch-anchor-inline" id="branch-inline-${msgIdx}-${bi}">
      <button class="branch-node inline${isActive ? " active" : ""}${isStreaming ? " streaming" : ""}"
        onclick="toggleBranchPanel(${msgIdx},${bi})" title="${escapeHtml(b.title)}">
        <span class="branch-node-icon">${icon}</span>
        <span class="branch-node-label">${escapeHtml(b.title.length > 50 ? b.title.slice(0, 48) + "…" : b.title)}</span>
        ${count ? `<span class="branch-node-count">${count}</span>` : ""}
        ${isStreaming ? '<span class="branch-node-pulse"></span>' : ""}
        <span class="branch-node-close" onclick="event.stopPropagation();branchCloseOrDelete(${msgIdx},${bi})" title="${isActive ? escapeHtml(t('branch.collapse')) : escapeHtml(t('branch.delete'))}">✕</span>
      </button>`;

    // If this anchored branch is expanded, render panel inline too
    if (isActive) {
      pillHtml += _renderBranchPanel(msg, msgIdx, bi);
    }
    pillHtml += `</div>`;

    out = out.slice(0, bestInsertPos) + pillHtml + out.slice(bestInsertPos);
    inlinedSet.add(bi);
  }
  return { html: out, inlinedSet };
}

// ══════════════════════════════════════════
//  Render branch zone — un-inlined branches + add button
// ══════════════════════════════════════════
function renderBranchZone(msg, msgIdx, inlinedSet) {
  const branches = msg.branches || [];
  const conv = getActiveConv();

  // Render un-inlined branch pills (those without anchors, or whose anchor wasn't found)
  const pills = branches.map((b, bi) => {
    if (inlinedSet && inlinedSet.has(bi)) return "";  // skip inlined ones
    const isActive = _activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === bi;
    const isStreaming = conv && _isBranchStreaming(conv.id, msgIdx, bi);
    const icon = b.icon || '';
    const count = (b.messages || []).filter(m => m.role === "user").length;
    return `<button class="branch-node${isActive ? " active" : ""}${isStreaming ? " streaming" : ""}"
      onclick="toggleBranchPanel(${msgIdx},${bi})" title="${escapeHtml(b.title)}">
      <span class="branch-node-icon">${icon}</span>
      <span class="branch-node-label">${escapeHtml(b.title.length > 20 ? b.title.slice(0, 18) + "…" : b.title)}</span>
      ${count ? `<span class="branch-node-count">${count}</span>` : ""}
      ${isStreaming ? '<span class="branch-node-pulse"></span>' : ""}
      <span class="branch-node-close" onclick="event.stopPropagation();branchCloseOrDelete(${msgIdx},${bi})" title="${isActive ? escapeHtml(t('branch.collapse')) : escapeHtml(t('branch.delete'))}">✕</span>
    </button>`;
  }).filter(Boolean);

  // Expanded panel — only for un-inlined active branch
  let panelHtml = "";
  if (_activeBranch?.msgIdx === msgIdx) {
    const bi = _activeBranch.branchIdx;
    if (!inlinedSet || !inlinedSet.has(bi)) {
      panelHtml = _renderBranchPanel(msg, msgIdx, bi);
    }
  }

  // ★ The "Add branch" (分支) affordance now lives in the unified bottom
  //   `.message-actions` bar (see chat_render.js) — the branch-zone only
  //   renders existing branch pills + the expanded panel. When there are
  //   none, it collapses to nothing.
  if (!pills.length && !panelHtml) {
    return "";
  }
  return `<div class="branch-zone"><div class="branch-nodes">${pills.join("")}</div>${panelHtml}</div>`;
}

// ══════════════════════════════════════════
//  Render a single branch message
// ══════════════════════════════════════════
function _renderBranchMsg(m, msgIdx, bi, i) {
  const isUser = m.role === "user";
  const roleLabel = isUser ? "You" : "✦ Claude";
  let content = "";

  // Reply quotes in branch messages
  const quotes = m.replyQuotes || (m.replyQuote ? [m.replyQuote] : []);
  for (const rq of quotes) {
    const rqP = rq.replace(/\s+/g, " ").slice(0, 60);
    content += `<div class="reply-quote-badge" style="margin-bottom:6px;font-size:11px" title="${escapeHtml(rq.slice(0, 200))}">

      <span class="reply-quote-badge-info"><span class="reply-quote-badge-name">${escapeHtml(rqP)}${rq.length > 60 ? "…" : ""}</span></span></div>`;
  }
  // Conversation reference badges in branch messages
  if (m.convRefs && m.convRefs.length > 0) {
    for (const cr of m.convRefs) {
      content += `<div class="reply-quote-badge conv-ref-badge" style="margin-bottom:6px;font-size:11px" title="${escapeHtml(t('chat.convRefTitle', { title: escapeHtml(cr.title || cr.id) }))}">
        <span class="reply-quote-badge-icon">@</span>
        <span class="reply-quote-badge-info"><span class="reply-quote-badge-name">${escapeHtml(cr.title || cr.id)}</span></span></div>`;
    }
  }

  if (isUser) {
    content += escapeHtml(m.content || "");
  } else {
    // Tool call results (search, browser, code exec, project tools) — use the full renderer
    const rounds = getToolRoundsFromMsg(m);
    if (rounds.length > 0) {
      content += renderToolRoundsHTML(rounds, false);
    }
    // Thinking
    if (m.thinking) {
      const bThinkLen = m.thinking.length;
      const bThinkMeta = bThinkLen >= 1024 ? ` (${Math.round(bThinkLen / 1024)}k chars)` : ` (${bThinkLen} chars)`;
      const _bThinkLbl = (typeof t === 'function') ? t('stream.thinking.done') : 'Thinking Process';
      content += `<details class="branch-thinking" data-branch-think-msgidx="${msgIdx}" data-branch-think-bidx="${bi}" data-branch-think-midx="${i}"><summary>${escapeHtml(_bThinkLbl)}${bThinkMeta}</summary><div class="branch-think-lazy"></div></details>`;
    }
    // Content
    try { content += renderMarkdown(m.content || ""); } catch { content += escapeHtml(m.content || ""); }
    // Finish info
    if (m.finishReason || m.preset) {
      const ef = m.preset || m.effort || "";
      content += `<div style="font-size:10px;color:var(--text-tertiary);margin-top:4px">${ef ? ef + " · " : ""}${m.finishReason || ""}</div>`;
    }
  }
  return `<div class="branch-msg ${isUser ? "user" : "assistant"}">
    <div class="branch-msg-header"><span class="branch-msg-role">${roleLabel}</span></div>
    <div class="branch-msg-body">${content}</div></div>`;
}

// ══════════════════════════════════════════
//  Render the expanded panel for a branch
// ══════════════════════════════════════════
function _renderBranchPanel(msg, msgIdx, bi) {
  const branch = msg.branches?.[bi];
  if (!branch) return "";
  const conv = getActiveConv();
  const msgs = branch.messages || [];
  const icon = branch.icon || '';
  const userCount = msgs.filter(m => m.role === "user").length;
  const bk = conv ? _branchKey(conv.id, msgIdx, bi) : "";
  const isStreaming = _branchStreams.has(bk);
  const hasPersistentTask = !isStreaming && !!branch.activeTaskId;

  // Render finished messages (skip last assistant if it's currently streaming)
  let msgsHtml = "";
  const renderMsgs = (isStreaming || hasPersistentTask)
    ? msgs.slice(0, -1)  // exclude the last assistant msg being streamed
    : msgs;
  for (let i = 0; i < renderMsgs.length; i++) {
    msgsHtml += _renderBranchMsg(renderMsgs[i], msgIdx, bi, i);
  }

  // Streaming zone
  let streamingHtml = "";
  if (isStreaming || hasPersistentTask) {
    const lastMsg = msgs[msgs.length - 1];
    const existingContent = lastMsg?.content || "";
    const existingThinking = lastMsg?.thinking || "";
    const _bStreamThinkLbl = (typeof t === 'function') ? t('stream.thinking.done') : 'Thinking Process';
    streamingHtml = `<div class="branch-msg assistant branch-streaming-msg" id="branch-streaming-${msgIdx}-${bi}">
      <div class="branch-msg-header"><span class="branch-msg-role">✦ Claude</span></div>
      <div class="branch-msg-body" id="branch-streaming-body-${msgIdx}-${bi}">
        <div data-zone="tool"></div>
        <div data-zone="thinking">${existingThinking ? `<details class="branch-thinking" open><summary>${escapeHtml(_bStreamThinkLbl)}</summary><div>${escapeHtml(existingThinking)}</div></details>` : ""}</div>
        <div data-zone="content">${existingContent ? (() => { try { return renderMarkdown(existingContent); } catch { return escapeHtml(existingContent); } })() : ""}</div>
        <div data-zone="status"><div class="stream-status"><div class="pulse"></div> Generating…</div></div>
      </div></div>`;
  }

  let emptyMsg = "";
  if (!msgs.length && !isStreaming) {
    const selCtx = branch.parentSelection
      ? `<div class="branch-selection-ctx">${escapeHtml(t('branch.selectionCtx', { text: escapeHtml(branch.parentSelection.slice(0, 120)) + (branch.parentSelection.length > 120 ? "…" : "") }))}</div>`
      : "";
    emptyMsg = `<div class="branch-empty">${selCtx}${escapeHtml(t('branch.emptyHint'))}</div>`;
  }

  return `<div class="branch-panel" id="branch-panel-${msgIdx}-${bi}">
    <div class="branch-panel-header">
      <span class="branch-panel-icon">${icon}</span>
      <span class="branch-panel-title">${escapeHtml(branch.title)}</span>
      <span class="branch-panel-count">${escapeHtml(t('branch.userTurns', { n: userCount }))}</span>
      <span class="branch-panel-tools" style="font-size:10px;opacity:0.5;margin-left:4px">${searchMode !== "off" ? "" : ""}${fetchEnabled ? "" : ""}${codeExecEnabled ? "⚡" : ""}${browserEnabled ? "" : ""}${memoryEnabled ? "" : ""}</span>
      ${(isStreaming || hasPersistentTask) ? `<button class="branch-panel-stop" onclick="stopBranchStream(${msgIdx},${bi})" title="${escapeHtml(t('branch.stopGen'))}">${escapeHtml(t('branch.stop'))}</button>` : ""}
      <button class="branch-panel-collapse" onclick="closeBranchPanel()" title="${escapeHtml(t('branch.collapseBranch'))}">▾ ${escapeHtml(t('branch.collapseCta'))}</button>
      <button class="branch-panel-delete" onclick="deleteBranch(${msgIdx},${bi})" title="${escapeHtml(t('branch.deleteBranch'))}"></button>
    </div>
    <div class="branch-messages" id="branch-messages-${msgIdx}-${bi}">
      ${emptyMsg}${msgsHtml}${streamingHtml}
    </div>
    <div class="branch-input-hint">${escapeHtml(t('branch.inputHint'))}</div>
  </div>`;
}

// ══════════════════════════════════════════
//  Toggle / Close / Delete branch panels
// ══════════════════════════════════════════
function toggleBranchPanel(msgIdx, branchIdx) {
  if (_activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === branchIdx) {
    closeBranchPanel();
    return;
  }
  // Close any previously open branch panel
  const prevBranch = _activeBranch;
  _activeBranch = { msgIdx, branchIdx };
  const conv = getActiveConv();
  if (conv) {
    // Hide the previously open panel (targeted update)
    if (prevBranch) {
      const prevPanelEl = document.getElementById(`branch-panel-${prevBranch.msgIdx}-${prevBranch.branchIdx}`);
      if (prevPanelEl) prevPanelEl.remove();
    }
    // Build and insert the new panel
    const msg = conv.messages[msgIdx];
    if (msg) _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);
    // Auto-reconnect if branch has a persistent task
    const branch = msg?.branches?.[branchIdx];
    if (branch?.activeTaskId && !_isBranchStreaming(conv.id, msgIdx, branchIdx)) {
      _reconnectBranchStream(conv, msgIdx, branchIdx, branch);
    }
    _enterBranchMode(msgIdx, branchIdx);
    updateSendButton();
    // Scroll the branch panel into view
    requestAnimationFrame(() => {
      const panel = document.getElementById(`branch-panel-${msgIdx}-${branchIdx}`);
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }
}

function closeBranchPanel() {
  const prev = _activeBranch;
  _activeBranch = null;
  _exitBranchMode();
  // Remove the panel from DOM without full re-render
  if (prev) {
    const panelEl = document.getElementById(`branch-panel-${prev.msgIdx}-${prev.branchIdx}`);
    if (panelEl) panelEl.remove();
  }
  updateSendButton();
}

// ── Smart close/delete: if panel is expanded → close; if collapsed → delete ──
function branchCloseOrDelete(msgIdx, branchIdx) {
  const isExpanded = _activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === branchIdx;
  if (isExpanded) {
    closeBranchPanel();
  } else {
    deleteBranch(msgIdx, branchIdx);
  }
}

async function deleteBranch(msgIdx, branchIdx) {
  if (!await showConfirm(t('branch.deleteConfirm'), { danger: true })) return;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg?.branches?.[branchIdx]) return;
  // Abort stream if running
  const bk = _branchKey(conv.id, msgIdx, branchIdx);
  const stream = _branchStreams.get(bk);
  if (stream) { stream.controller.abort(); _branchStreams.delete(bk); activeStreams.delete(bk); }

  // Try inline DOM removal first (avoids full re-render and scroll jump)
  const targetInlineEl = document.getElementById(`branch-inline-${msgIdx}-${branchIdx}`);
  // Also check if the branch-zone has a pill for non-inlined branches
  const panelEl = document.getElementById(`branch-panel-${msgIdx}-${branchIdx}`);

  // Optimistic local splice — server confirms via DELETE below.
  const _prevBranches = msg.branches.slice();
  msg.branches.splice(branchIdx, 1);
  if (_activeBranch?.msgIdx === msgIdx) { _activeBranch = null; _exitBranchMode(); }

  // Remove DOM elements directly — zero scroll impact
  if (targetInlineEl) targetInlineEl.remove();
  if (panelEl && !panelEl.closest('.branch-anchor-inline')) panelEl.remove(); // standalone panel

  saveConversations(conv.id);
  // ★ Targeted DELETE — replaces full-conversation PUT so siblings and
  //   anchor text stay intact on the server. On error we restore the
  //   in-memory branches array and force a re-render from server state.
  (async () => {
    /* Shared rollback: server rejection AND network failure both restore the
     *   splice and resync from the server — a network failure may or may not
     *   have landed the delete, and only the server state is the truth
     *   (pt_3cd6cd48: the old catch left local/server diverged). */
    const _revertAndResync = async (why, extra) => {
      console.warn('[branch.delete]', why, extra || '');
      msg.branches = _prevBranches;
      saveConversations(conv.id);
      try {
        const data = await Api.conversations.get(conv.id);
        if (data && Array.isArray(data.messages)) {
          conv.messages = data.messages;
          saveConversations(conv.id);
          if (activeConvId === conv.id) window.ConvView.replaceAll(conv.id);
        }
      } catch (e2) { console.warn('[branch.delete] reload failed', e2); }
      if (typeof showToast === 'function') showToast('Branch delete failed — restored', 'error');
    };
    try {
      const res = await Api.conversations.deleteBranch(conv.id, msgIdx, branchIdx, { msgId: msg._msgId });
      if (!res || !res.ok) {
        let body = null;
        try { body = res ? await res.json() : null; } catch (_e) { /* ignore */ }
        await _revertAndResync('server rejected branch delete', [res && res.status, body]);
      }
    } catch (e) {
      await _revertAndResync('network error', e && e.message);
    }
  })();

  // Update IDs of remaining inline branch elements (indices shifted after splice)
  const msgEl = document.getElementById(`msg-${msgIdx}`);
  if (msgEl) {
    const inlineEls = msgEl.querySelectorAll('.branch-anchor-inline');
    inlineEls.forEach(el => {
      // Re-map IDs: find the branch this element belongs to by matching anchor text or sequential order
      const oldId = el.id; // e.g. "branch-inline-5-3"
      const match = oldId.match(/^branch-inline-(\d+)-(\d+)$/);
      if (match) {
        const oldBi = parseInt(match[2], 10);
        if (oldBi > branchIdx) {
          const newBi = oldBi - 1;
          el.id = `branch-inline-${msgIdx}-${newBi}`;
          // Update onclick handlers in child buttons
          el.querySelectorAll('button[onclick]').forEach(btn => {
            btn.setAttribute('onclick',
              btn.getAttribute('onclick')
                .replace(`toggleBranchPanel(${msgIdx},${oldBi})`, `toggleBranchPanel(${msgIdx},${newBi})`)
                .replace(`branchCloseOrDelete(${msgIdx},${oldBi})`, `branchCloseOrDelete(${msgIdx},${newBi})`)
            );
          });
          // Update panel ID if present
          const panel = el.querySelector('.branch-panel');
          if (panel) panel.id = `branch-panel-${msgIdx}-${newBi}`;
        }
      }
    });
  }

  // Re-render branch zone for the non-inlined branches (add button, remaining pills)
  const inlinedSet = new Set();
  (msg.branches || []).forEach((b, bi) => { if (b.anchorText) inlinedSet.add(bi); });
  const zoneEl = document.querySelector(`#msg-${msgIdx} .branch-zone`);
  if (zoneEl) {
    const tmp = document.createElement("div");
    tmp.innerHTML = renderBranchZone(msg, msgIdx, inlinedSet);
    zoneEl.replaceWith(tmp.firstElementChild || tmp);
  }
}

// ══════════════════════════════════════════
//  Stop a branch stream
// ══════════════════════════════════════════
function stopBranchStream(msgIdx, branchIdx) {
  const conv = getActiveConv();
  if (!conv) return;
  const bk = _branchKey(conv.id, msgIdx, branchIdx);
  const stream = _branchStreams.get(bk);
  if (stream) {
    stream.controller.abort();
    _branchStreams.delete(bk);
    activeStreams.delete(bk);
  }
  // Finalize UI
  const msg = conv.messages[msgIdx];
  const branch = msg?.branches?.[branchIdx];
  if (branch) {
    // Remove empty trailing assistant message if content is empty
    const msgs = branch.messages || [];
    const last = msgs[msgs.length - 1];
    if (last?.role === "assistant" && !last.content?.trim()) {
      msgs.pop();
    }
  }
  saveConversations(conv.id);
  _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);
  updateSendButton();
}

// ══════════════════════════════════════════
//  Branch mode — hijack the main input bar
// ══════════════════════════════════════════
function _enterBranchMode(msgIdx, branchIdx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  const branch = msg?.branches?.[branchIdx];
  if (!branch) return;
  const icon = branch.icon || '';
  const input = document.getElementById("userInput");
  if (input) input.placeholder = t('branch.inputPlaceholder', { title: branch.title });

  // Add banner above input box
  let banner = document.getElementById("branch-mode-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "branch-mode-banner";
    banner.className = "branch-mode-banner";
    const inputBox = document.querySelector(".input-box");
    if (inputBox) inputBox.parentElement.insertBefore(banner, inputBox);
  }
  banner.innerHTML = `<span class="branch-mode-banner-icon">${icon}</span>
    <span class="branch-mode-banner-text">${escapeHtml(t('branch.modeBanner', { title: escapeHtml(branch.title) }))}</span>
    <button class="branch-mode-banner-exit" onclick="closeBranchPanel()">✕ ${escapeHtml(t('branch.exit'))}</button>`;
  banner.style.display = "flex";

  // Scroll branch panel to bottom
  _scrollBranchToBottom(msgIdx, branchIdx);
}

function _exitBranchMode() {
  const banner = document.getElementById("branch-mode-banner");
  if (banner) banner.style.display = "none";
  const input = document.getElementById("userInput");
  if (input) input.placeholder = "Ask me anything… (Enter)";
}

function isBranchModeActive() {
  return _activeBranch !== null;
}

function getActiveBranchContext() {
  return _activeBranch;
}

// ══════════════════════════════════════════
//  Send message in branch
// ══════════════════════════════════════════
async function sendBranchMessage(text, images) {
  const branchCtx = _activeBranch;
  if (!branchCtx) return;
  const { msgIdx, branchIdx } = branchCtx;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  const branch = msg?.branches?.[branchIdx];
  if (!branch) return;

  const bk = _branchKey(conv.id, msgIdx, branchIdx);

  // Don't double-send on THIS branch
  if (_branchStreams.has(bk)) return;

  if (!branch.messages) branch.messages = [];

  // Build user message
  const userMsg = { role: "user", content: text, timestamp: Date.now() };
  if (images?.length > 0) userMsg.images = images;

  // Attach pending reply quotes
  if (typeof getPendingReplyQuotes === "function") {
    const rqs = getPendingReplyQuotes();
    if (rqs?.length > 0) { userMsg.replyQuotes = rqs; clearReplyQuote(); }
  }

  // Add empty assistant message (placeholder for streaming)
  const assistantMsg = {
    role: "assistant", content: "", thinking: "",
    timestamp: Date.now(), toolRounds: [],
  };
  branch.messages.push(userMsg, assistantMsg);
  saveConversations(conv.id);
  // ★ Must await sync so the backend can load fresh branch messages from DB
  await syncConversationToServer(conv);

  // ★ Server-side message building: the backend loads the conversation from DB,
  //   extracts main chat context + branch messages, and runs the full transform
  //   pipeline — message building is fully server-side.
  // ★ _buildConvConfig is async (resolved by /api/v1/conversations/config/resolve).
  const _branchConfig = await _buildConvConfig(conv);
  _branchConfig.branchKey = bk;
  const body = {
    convId: conv.id,
    msgIdx,
    branchIdx,
    config: _branchConfig,
  };

  // ── Targeted DOM update: show the new user+assistant messages in the branch panel ──
  if (activeConvId === conv.id) _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);

  console.log("[Branch] sendBranchMessage config:", _branchConfig);

  try {
    const data = await Api.chat.branchStart(body);
    if (!data) throw new Error('No response from chat/branch/start');
    const taskId = data.taskId;
    if (!taskId) throw new Error("No taskId returned");

    // Persist taskId on the branch so it survives refresh
    branch.activeTaskId = taskId;
    saveConversations(conv.id);

    // Register stream
    const controller = new AbortController();
    _branchStreams.set(bk, { controller, taskId, convId: conv.id, msgIdx, branchIdx });
    activeStreams.set(bk, { controller, taskId });
    renderConversationList();
    updateSendButton();

    // ── Targeted DOM update: rebuild ONLY the branch panel, no full renderChat ──
    _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);
    _enterBranchMode(msgIdx, branchIdx);

    // Start SSE stream
    try {
      await _branchStreamSSE(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk);
    } catch (e) {
      if (e.name !== "AbortError") {
        console.error("Branch stream error:", e);
        if (!assistantMsg.content) assistantMsg.content = `Error: ${e.message}`;
      }
    } finally {
      _finishBranchStream(conv, msgIdx, branchIdx, branch, bk);
    }
  } catch (e) {
    console.error("Branch send error:", e);
    const last = branch.messages[branch.messages.length - 1];
    if (last?.role === "assistant" && !last.content) last.content = `${e.message}`;
    saveConversations(conv.id);
    if (activeConvId === conv.id) _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);
  }
}

// ══════════════════════════════════════════
//  Create a new branch
// ══════════════════════════════════════════
async function promptNewBranch(msgIdx, preTitle, selectedText, selectionRange) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg) return;
  // Branches are primarily for assistant messages, but allow user messages too
  if (msg.role === "user" && !selectedText) return;
  const title = preTitle || await showPrompt(t('branch.namePrompt'));
  if (!title?.trim()) return;

  // Server-authoritative branch creation: ID, icon, kind, validation,
  // persistence — all on the server. We just receive the new branch dict
  // and place it locally so the rest of the UI flow (DOM insertion,
  // _activeBranch tracking) keeps working.
  const created = await _createBranchOnServer(
    conv.id, msgIdx, title.trim(),
    selectedText ? selectedText.slice(0, 200) : '',
    selectedText || '',
  );
  if (!created) {
    await showAlert(t('branch.createFailed'));
    return;
  }
  const branch = created.branch;
  const bi = created.branchIdx;

  // Mirror server state locally for instant render. Server is the
  // source of truth — a subsequent syncConversationToServer pull would
  // re-fetch this same data.
  if (!msg.branches) msg.branches = [];
  // The server appended at branchIdx; ensure local array agrees.
  while (msg.branches.length < bi) msg.branches.push(null);
  msg.branches[bi] = branch;

  _activeBranch = { msgIdx, branchIdx: bi };

  // ── DOM insertion: use the actual Selection Range to place branch right after selected text ──
  let inlineSuccess = false;
  if (selectionRange) {
    try {
      // Verify the range is inside our message element
      const msgEl = document.getElementById("msg-" + msgIdx);
      if (msgEl && msgEl.contains(selectionRange.endContainer)) {
        // Collapse range to end of selection (cursor at the end of selected text)
        selectionRange.collapse(false);

        // Build the branch anchor wrapper as a real DOM element
        const wrapper = document.createElement("div");
        wrapper.className = "branch-anchor-inline";
        wrapper.id = `branch-inline-${msgIdx}-${bi}`;

        // Pill button
        const pillHtml = `<button class="branch-node inline active"
          onclick="toggleBranchPanel(${msgIdx},${bi})" title="${escapeHtml(branch.title)}">
          <span class="branch-node-icon">${branch.icon}</span>
          <span class="branch-node-label">${escapeHtml(branch.title.length > 48 ? branch.title.slice(0, 46) + "…" : branch.title)}</span>
          <span class="branch-node-close" onclick="event.stopPropagation();branchCloseOrDelete(${msgIdx},${bi})" title="${escapeHtml(t('branch.collapse'))}">✕</span>
        </button>`;
        wrapper.innerHTML = pillHtml;

        // Find the block-level ancestor (p, li, pre, div, blockquote, etc.) to insert AFTER
        let insertAfter = selectionRange.endContainer;
        if (insertAfter.nodeType === Node.TEXT_NODE) insertAfter = insertAfter.parentNode;
        // Walk up to the nearest block-level element inside .message-body or .md-content
        const bodyEl = msgEl.querySelector(".message-body");
        while (insertAfter && insertAfter !== bodyEl && insertAfter.parentNode !== bodyEl) {
          const mdContent = msgEl.querySelector(".md-content");
          if (mdContent && insertAfter.parentNode === mdContent) break;
          insertAfter = insertAfter.parentNode;
        }

        // Insert the wrapper right after the block element containing the selection
        if (insertAfter && insertAfter.parentNode) {
          insertAfter.parentNode.insertBefore(wrapper, insertAfter.nextSibling);
          inlineSuccess = true;
        }
      }
    } catch (e) {
      console.warn("Branch inline insertion failed:", e);
    }
  }

  // Fallback: place in branch-zone at bottom of message
  if (!inlineSuccess) {
    _rebuildBranchPanelDOM(msg, msgIdx, bi);
  }

  // Now build and insert the expanded panel right inside the wrapper (or after pill)
  if (inlineSuccess) {
    const wrapper = document.getElementById(`branch-inline-${msgIdx}-${bi}`);
    if (wrapper) {
      const panelHtml = _renderBranchPanel(msg, msgIdx, bi);
      const tmp = document.createElement("div");
      tmp.innerHTML = panelHtml;
      const panelEl = tmp.firstElementChild;
      if (panelEl) wrapper.appendChild(panelEl);
    }
  }

  _enterBranchMode(msgIdx, bi);

  // Scroll the branch panel into view smoothly
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      const panel = document.getElementById(`branch-panel-${msgIdx}-${bi}`);
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });

  // Server persisted the branch on the create POST above; we only need
  // to refresh the local IDB cache so a page-refresh sees it without
  // a server round-trip.  No PUT-sync — server is already the source
  // of truth for branches.
  setTimeout(() => { saveConversations(conv.id); }, 100);
}

// ══════════════════════════════════════════
//  Text Selection Popup — Branch / Reply
// ══════════════════════════════════════════
let _selectionPopup = null;
let _pendingReplyQuotes = [];  // array of quote strings


function _scrollBranchToBottom(msgIdx, branchIdx) {
  const container = document.getElementById(`branch-messages-${msgIdx}-${branchIdx}`);
  if (container) container.scrollTop = container.scrollHeight;
}


