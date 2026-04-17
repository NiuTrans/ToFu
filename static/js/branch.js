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
function _branchAutoIcon(title) {
  if (!title) return "";
  const t = title.toLowerCase();
  if (/paper|论文|arxiv/.test(t)) return "";
  if (/code|代码|实现|implement/.test(t)) return "";
  if (/data|数据|dataset/.test(t)) return "";
  if (/math|公式|proof|证明/.test(t)) return "";
  if (/image|图|visual|vision/.test(t)) return "";
  if (/compare|对比|vs\.?/.test(t)) return "";
  if (/bug|error|issue|问题/.test(t)) return "";
  if (/todo|plan|计划/.test(t)) return "";
  if (/idea|想法|thought/.test(t)) return "";
  if (/summary|总结|概述/.test(t)) return "";
  return "";
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
    const icon = b.icon || _branchAutoIcon(b.title);
    const count = (b.messages || []).filter(m => m.role === "user").length;

    let pillHtml = `<div class="branch-anchor-inline" id="branch-inline-${msgIdx}-${bi}">
      <button class="branch-node inline${isActive ? " active" : ""}${isStreaming ? " streaming" : ""}"
        onclick="toggleBranchPanel(${msgIdx},${bi})" title="${escapeHtml(b.title)}">
        <span class="branch-node-icon">${icon}</span>
        <span class="branch-node-label">${escapeHtml(b.title.length > 50 ? b.title.slice(0, 48) + "…" : b.title)}</span>
        ${count ? `<span class="branch-node-count">${count}</span>` : ""}
        ${isStreaming ? '<span class="branch-node-pulse"></span>' : ""}
        <span class="branch-node-close" onclick="event.stopPropagation();branchCloseOrDelete(${msgIdx},${bi})" title="${isActive ? '收起' : '删除'}">✕</span>
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
    const icon = b.icon || _branchAutoIcon(b.title);
    const count = (b.messages || []).filter(m => m.role === "user").length;
    return `<button class="branch-node${isActive ? " active" : ""}${isStreaming ? " streaming" : ""}"
      onclick="toggleBranchPanel(${msgIdx},${bi})" title="${escapeHtml(b.title)}">
      <span class="branch-node-icon">${icon}</span>
      <span class="branch-node-label">${escapeHtml(b.title.length > 20 ? b.title.slice(0, 18) + "…" : b.title)}</span>
      ${count ? `<span class="branch-node-count">${count}</span>` : ""}
      ${isStreaming ? '<span class="branch-node-pulse"></span>' : ""}
      <span class="branch-node-close" onclick="event.stopPropagation();branchCloseOrDelete(${msgIdx},${bi})" title="${isActive ? '收起' : '删除'}">✕</span>
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

  const addBtn = `<button class="branch-add-btn" onclick="promptNewBranch(${msgIdx})" title="Add branch">分支</button>`;

  if (!pills.length && !panelHtml) {
    return `<div class="branch-zone">${addBtn}</div>`;
  }
  return `<div class="branch-zone"><div class="branch-nodes">${pills.join("")}${addBtn}</div>${panelHtml}</div>`;
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
      content += `<div class="reply-quote-badge conv-ref-badge" style="margin-bottom:6px;font-size:11px" title="引用对话: ${escapeHtml(cr.title || cr.id)}">
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
      content += `<details class="branch-thinking" data-branch-think-msgidx="${msgIdx}" data-branch-think-bidx="${bi}" data-branch-think-midx="${i}"><summary>Thinking Process${bThinkMeta}</summary><div class="branch-think-lazy"></div></details>`;
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
  const icon = branch.icon || _branchAutoIcon(branch.title);
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
    streamingHtml = `<div class="branch-msg assistant branch-streaming-msg" id="branch-streaming-${msgIdx}-${bi}">
      <div class="branch-msg-header"><span class="branch-msg-role">✦ Claude</span></div>
      <div class="branch-msg-body" id="branch-streaming-body-${msgIdx}-${bi}">
        <div data-zone="tool"></div>
        <div data-zone="thinking">${existingThinking ? `<details class="branch-thinking" open><summary>Thinking</summary><div>${escapeHtml(existingThinking)}</div></details>` : ""}</div>
        <div data-zone="content">${existingContent ? (() => { try { return renderMarkdown(existingContent); } catch { return escapeHtml(existingContent); } })() : ""}</div>
        <div data-zone="status"><div class="stream-status"><div class="pulse"></div> Generating…</div></div>
      </div></div>`;
  }

  // Approval buttons
  const stream = _branchStreams.get(bk);
  const lastAssistant = msgs[msgs.length - 1];
  let approvalHtml = "";
  if (lastAssistant?.approvalRequired) {
    approvalHtml = `<div class="branch-approval">
      <span>Tool needs approval</span>
      <button class="branch-approve-btn" onclick="approveBranchTool(${msgIdx},${bi},'approve')">Approve</button>
      <button class="branch-reject-btn" onclick="approveBranchTool(${msgIdx},${bi},'deny')">Deny</button></div>`;
  }

  let emptyMsg = "";
  if (!msgs.length && !isStreaming) {
    const selCtx = branch.parentSelection
      ? `<div class="branch-selection-ctx">选中内容：「${escapeHtml(branch.parentSelection.slice(0, 120))}${branch.parentSelection.length > 120 ? "…" : ""}」</div>`
      : "";
    emptyMsg = `<div class="branch-empty">${selCtx}点击底部输入框发送消息开始分支对话</div>`;
  }

  return `<div class="branch-panel" id="branch-panel-${msgIdx}-${bi}">
    <div class="branch-panel-header">
      <span class="branch-panel-icon">${icon}</span>
      <span class="branch-panel-title">${escapeHtml(branch.title)}</span>
      <span class="branch-panel-count">${userCount}条对话</span>
      <span class="branch-panel-tools" style="font-size:10px;opacity:0.5;margin-left:4px">${searchMode !== "off" ? "" : ""}${fetchEnabled ? "" : ""}${codeExecEnabled ? "⚡" : ""}${browserEnabled ? "" : ""}${memoryEnabled ? "" : ""}</span>
      ${(isStreaming || hasPersistentTask) ? `<button class="branch-panel-stop" onclick="stopBranchStream(${msgIdx},${bi})" title="停止生成">停止</button>` : ""}
      <button class="branch-panel-collapse" onclick="closeBranchPanel()" title="收起分支">▾ 收起</button>
      <button class="branch-panel-delete" onclick="deleteBranch(${msgIdx},${bi})" title="删除分支"></button>
    </div>
    <div class="branch-messages" id="branch-messages-${msgIdx}-${bi}">
      ${emptyMsg}${msgsHtml}${streamingHtml}${approvalHtml}
    </div>
    <div class="branch-input-hint">主输入框已切换为分支模式 — 在下方输入并发送</div>
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

function deleteBranch(msgIdx, branchIdx) {
  // Preserve scroll before confirm dialog (native dialog can cause reflow)
  const container = document.getElementById("chatContainer");
  const savedScrollTop = container ? container.scrollTop : 0;
  if (!confirm("删除这个分支？")) {
    // Restore scroll if user cancelled
    if (container) container.scrollTop = savedScrollTop;
    return;
  }
  // Restore scroll immediately after confirm dialog closes
  if (container) container.scrollTop = savedScrollTop;
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

  msg.branches.splice(branchIdx, 1);
  if (_activeBranch?.msgIdx === msgIdx) { _activeBranch = null; _exitBranchMode(); }

  // Remove DOM elements directly — zero scroll impact
  if (targetInlineEl) targetInlineEl.remove();
  if (panelEl && !panelEl.closest('.branch-anchor-inline')) panelEl.remove(); // standalone panel

  saveConversations(conv.id);
  syncConversationToServer(conv);

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
  const icon = branch.icon || _branchAutoIcon(branch.title);
  const input = document.getElementById("userInput");
  if (input) input.placeholder = `在「${branch.title}」分支中输入消息…`;

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
    <span class="branch-mode-banner-text">分支: ${escapeHtml(branch.title)}</span>
    <button class="branch-mode-banner-exit" onclick="closeBranchPanel()">✕ 退出</button>`;
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
  const _branchConfig = (typeof _buildConvConfig === 'function') ? _buildConvConfig(conv) : {
    model: serverModel,
    maxTokens: config.maxTokens,
    thinkingEnabled,
    temperature: config.temperature,
    searchMode,
    fetchEnabled,
    codeExecEnabled,
    memoryEnabled,
    projectPath: conv.projectPath || "",
  };
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
    const res = await fetch(apiUrl("/api/chat/branch/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const errText = await res.text().catch(() => "");
      console.error("[Branch] HTTP error:", res.status, errText);
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
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
//  Build API messages for branch — REMOVED
//  ★ Branch messages are now built server-side by
//    build_branch_api_messages() in conv_message_builder.py,
//    called via POST /api/chat/branch/start.
// ══════════════════════════════════════════

// ══════════════════════════════════════════
//  Branch SSE streaming
// ══════════════════════════════════════════
async function _branchStreamSSE(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk) {
  let lastSave = Date.now();
  let gotData = false;
  const sseTimeout = setTimeout(() => { if (!gotData) controller.abort(); }, 45000);

  function _processEvent(ev) {
    gotData = true;
    if (ev.type !== "delta") console.log("[Branch SSE]", ev.type, ev);

    if (ev.type === "delta") {
      // Delta events APPEND to existing content (matching main chat behavior)
      if (typeof ev.thinking === "string") {
        assistantMsg.thinking = (assistantMsg.thinking || "") + ev.thinking;
      }
      if (typeof ev.content === "string") {
        assistantMsg.content = (assistantMsg.content || "") + ev.content;
      }
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "state") {
      // Full state snapshot — replace everything
      if (ev.content !== undefined) assistantMsg.content = ev.content;
      if (ev.thinking !== undefined) assistantMsg.thinking = ev.thinking;
      if (ev.error) assistantMsg.error = ev.error;
      if (ev.toolRounds) assistantMsg.toolRounds = ev.toolRounds;
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "phase") {
      // Update status indicator
      let statusHtml = "";
      if (ev.phase === "thinking") {
        statusHtml = '<div class="stream-status"><div class="pulse"></div> Thinking…</div>';
      } else if (ev.phase === "responding") {
        statusHtml = '<div class="stream-status"><div class="pulse"></div> Responding…</div>';
      } else if (ev.phase === "searching") {
        statusHtml = '<div class="stream-status"><div class="pulse"></div> Searching…</div>';
      }
      const body = document.getElementById(`branch-streaming-body-${msgIdx}-${branchIdx}`);
      if (body) {
        const zone = body.querySelector('[data-zone="status"]');
        if (zone) zone.innerHTML = statusHtml;
      }
    } else if (ev.type === "tool_start") {
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      assistantMsg.toolRounds.push({
        roundNum: ev.roundNum,
        query: ev.query || ev.toolName || "",
        toolName: ev.toolName || "search",
        status: "searching",
        results: [],
      });
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "tool_result") {
      const r = (assistantMsg.toolRounds || []).find(r => r.roundNum === ev.roundNum);
      if (r) {
        r.results = ev.results;
        r.status = "done";
        r.approvalId = null;
        if (ev.searchDiag) r.searchDiag = ev.searchDiag;
        if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
      }
      /* ★ Toast for create_memory */
      if (ev.results && ev.results.some(r => r.toolName === 'create_memory')) {
        const sk = ev.results.find(r => r.toolName === 'create_memory');
        const ok = sk.memoryOk === true || (sk.badge && sk.badge.includes('saved'));
        if (typeof showToast === 'function') {
          const sName = sk.memoryName || 'Memory';
          const sScope = sk.memoryScope || 'project';
          const title = ok ? `${sName}` : 'Memory Failed';
          const body = ok
            ? `Saved to ${sScope} scope — available in future sessions`
            : (sk.snippet || sk.title || 'Unknown error');
          showToast('', title, body, ok ? 5000 : 8000);
        }
      }
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "tool_complete") {
      // Store toolContent on the round for preview
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(r => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId);
        if (r) r.toolContent = ev.toolContent || null;
      }
      // ★ Re-render branch UI so preview button appears reactively
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "emit_ref") {
      // ★ emit_to_user: store emitted tool content on message for inline rendering
      assistantMsg._emitContent = ev.emitContent || '';
      assistantMsg._emitToolName = ev.emitToolName || '';
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "approval_required") {
      assistantMsg.approvalRequired = true;
      // Targeted update: rebuild only the branch panel to show approval buttons
      if (activeConvId === conv.id) {
        const parentMsg = conv.messages[msgIdx];
        if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      }
    } else if (ev.type === "done") {
      /* ★ DIAGNOSTIC: log endpoint/swarm done event */
      console.log(
        `[processEvent/endpoint] DONE — ` +
        `finishReason=${ev.finishReason || 'none'} ` +
        `contentLen=${assistantMsg.content?.length || 0} ` +
        `error=${ev.error || 'none'} model=${ev.model || 'unknown'}`
      );
      if (ev._diagnostics) {
        console.warn(`[processEvent/endpoint]  SERVER DIAGNOSTICS:`, ev._diagnostics);
      }
      if (ev.error) assistantMsg.error = ev.error;
      if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
      if (ev.model) assistantMsg.model = ev.model;
      else if (ev.preset) assistantMsg.model = ev.preset;
      else if (ev.effort) assistantMsg.model = ev.effort;
      if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
      if (ev.toolSummary) assistantMsg.toolSummary = ev.toolSummary;
      if (ev.usage) assistantMsg.usage = ev.usage;
      assistantMsg.approvalRequired = false;
      return "done";
    } else if (ev.type === "error") {
      console.error(`[processEvent/endpoint] ERROR event: ${ev.error || ev.message || 'unknown'}`);
      assistantMsg.error = ev.error || ev.message || "Unknown error";
      return "done";
    }

    // Periodic save (every 3s, like main chat)
    var _brNow = Date.now();
    if (_brNow - lastSave > 3000) {
      lastSave = _brNow;
      saveConversations(conv.id);
    }
    /* ★ No cache write during streaming — server checkpoints to DB every 5s */
    return null;
  }

  // SSE fetch
  try {
    const res = await fetch(apiUrl(`/api/chat/stream/${taskId}`), {
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`SSE HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // Process remaining buffer
        if (buffer.trim()) {
          for (const line of buffer.split("\n")) {
            const l = line.trim();
            if (l.startsWith("data: ")) {
              try {
                const ev = JSON.parse(l.slice(6));
                if (_processEvent(ev) === "done") break;
              } catch {}
            }
          }
        }
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        const l = line.trim();
        if (!l.startsWith("data: ")) continue;
        try {
          const ev = JSON.parse(l.slice(6));
          if (_processEvent(ev) === "done") return;
        } catch {}
      }
    }
  } catch (e) {
    clearTimeout(sseTimeout);
    if (e.name === "AbortError") throw e;
    console.warn("Branch SSE failed, falling back to poll:", e.message);
    await _branchStreamPoll(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk);
  } finally {
    clearTimeout(sseTimeout);
  }
}

// ── Polling fallback ──
async function _branchStreamPoll(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk) {
  let retries = 0;
  while (retries < 120) {
    if (controller.signal.aborted) return;
    await new Promise(r => setTimeout(r, 1500));
    try {
      const res = await fetch(apiUrl(`/api/chat/poll/${taskId}`));
      if (!res.ok) {
        if (res.status === 404) {
          // Task gone (server restarted / cleaned up) — stop polling
          console.warn(`[_branchStreamPoll] 404 for task ${taskId.slice(0,8)} — stopping`);
          return;
        }
        retries++;
        continue;
      }
      const data = await res.json();
      if (data.thinking !== undefined) assistantMsg.thinking = data.thinking;
      if (data.content !== undefined) assistantMsg.content = data.content;
      if (data.toolRounds) assistantMsg.toolRounds = data.toolRounds;
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
      if (Date.now() - (assistantMsg._lastSave || 0) > 3000) {
        assistantMsg._lastSave = Date.now();
        saveConversations(conv.id);
      }
      if (data.status === "done" || data.status === "error") {
        if (data.error) assistantMsg.error = data.error;
        if (data.finishReason) assistantMsg.finishReason = data.finishReason;
        if (data.model) assistantMsg.model = data.model;
        else if (data.preset) assistantMsg.model = data.preset;
        if (data.toolSummary) assistantMsg.toolSummary = data.toolSummary;
        if (data.usage) assistantMsg.usage = data.usage;
        return;
      }
      retries = 0;
    } catch { retries++; }
  }
}

// ── Update streaming UI zones ──
function _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg) {
  const body = document.getElementById(`branch-streaming-body-${msgIdx}-${branchIdx}`);
  if (!body) return;

  // Tool call zone — use the full renderer for rich display
  const toolZone = body.querySelector('[data-zone="tool"]');
  if (toolZone && assistantMsg.toolRounds?.length) {
    toolZone.innerHTML = renderToolRoundsHTML(assistantMsg.toolRounds, true);
  }

  // Thinking zone
  const thinkZone = body.querySelector('[data-zone="thinking"]');
  if (thinkZone && assistantMsg.thinking) {
    thinkZone.innerHTML = `<details class="branch-thinking" open><summary>Thinking Process</summary><div>${escapeHtml(assistantMsg.thinking)}</div></details>`;
  }

  // Content zone
  const contentZone = body.querySelector('[data-zone="content"]');
  if (contentZone && assistantMsg.content) {
    try { contentZone.innerHTML = renderMarkdown(assistantMsg.content); }
    catch { contentZone.innerHTML = escapeHtml(assistantMsg.content); }
  }

  _scrollBranchToBottom(msgIdx, branchIdx);

  // Keep stop button in sync during branch streaming
  updateSendButton();
}

// ── Rebuild only the branch panel DOM without touching the main chat ──
function _rebuildBranchPanelDOM(msg, msgIdx, branchIdx) {
  const panelEl = document.getElementById(`branch-panel-${msgIdx}-${branchIdx}`);
  const newHtml = _renderBranchPanel(msg, msgIdx, branchIdx);
  if (panelEl) {
    // Replace existing panel in-place
    const tmp = document.createElement("div");
    tmp.innerHTML = newHtml;
    const newPanel = tmp.firstElementChild;
    if (newPanel) panelEl.replaceWith(newPanel);
    return;
  }
  // Panel doesn't exist yet — find the right insertion point
  const msgEl = document.getElementById(`msg-${msgIdx}`);
  if (!msgEl) return;

  // For anchored/inline branches, insert inside the wrapper element
  const inlineWrapper = document.getElementById(`branch-inline-${msgIdx}-${branchIdx}`);
  if (inlineWrapper) {
    const tmp = document.createElement("div");
    tmp.innerHTML = newHtml;
    const newPanel = tmp.firstElementChild;
    if (newPanel) inlineWrapper.appendChild(newPanel);
    return;
  }

  // Fallback: find or create branch-zone inside this message
  let zone = msgEl.querySelector(".branch-zone");
  if (!zone) {
    zone = document.createElement("div");
    zone.className = "branch-zone";
    const content = msgEl.querySelector(".message-content");
    if (content) content.appendChild(zone);
    else msgEl.appendChild(zone);
  }
  const tmp = document.createElement("div");
  tmp.innerHTML = newHtml;
  const newPanel = tmp.firstElementChild;
  if (newPanel) zone.appendChild(newPanel);
}

// ── Finish branch stream — cleanup ──
function _finishBranchStream(conv, msgIdx, branchIdx, branch, bk) {
  _branchStreams.delete(bk);
  activeStreams.delete(bk);
  branch.activeTaskId = null;
  renderConversationList();
  saveConversations(conv.id);
  syncConversationToServer(conv);
  updateSendButton();
  // ── Targeted DOM update: rebuild ONLY the branch panel, no full renderChat ──
  if (activeConvId === conv.id) {
    const msg = conv.messages[msgIdx];
    if (msg) _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);
    // Re-enter branch mode if this branch panel is still open
    if (_activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === branchIdx) {
      _enterBranchMode(msgIdx, branchIdx);
    }
  }
}

// ══════════════════════════════════════════
//  Reconnect to branch streams after page refresh
// ══════════════════════════════════════════
async function _reconnectBranchStream(conv, msgIdx, branchIdx, branch) {
  const bk = _branchKey(conv.id, msgIdx, branchIdx);
  if (_branchStreams.has(bk)) return; // already streaming

  const taskId = branch.activeTaskId;
  if (!taskId) return;

  try {
    const res = await fetch(apiUrl(`/api/chat/poll/${taskId}`));
    if (!res.ok) {
      branch.activeTaskId = null;
      saveConversations(conv.id);
      if (activeConvId === conv.id) {
        const parentMsg = conv.messages[msgIdx];
        if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      }
      return;
    }
    const data = await res.json();

    // If already done, apply final state
    if (data.status === "done" || data.status === "error") {
      const msgs = branch.messages || [];
      const last = msgs[msgs.length - 1];
      if (last?.role === "assistant") {
        if (data.content !== undefined) last.content = data.content;
        if (data.thinking !== undefined) last.thinking = data.thinking;
        if (data.error) last.error = data.error;
        if (data.finishReason) last.finishReason = data.finishReason;
        if (data.preset) last.preset = data.preset;
        if (data.toolSummary) last.toolSummary = data.toolSummary;
        if (data.usage) last.usage = data.usage;
        if (data.toolRounds) last.toolRounds = data.toolRounds;
      }
      branch.activeTaskId = null;
      saveConversations(conv.id);
      if (activeConvId === conv.id) {
        const parentMsg = conv.messages[msgIdx];
        if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      }
      return;
    }

    // Still running — reconnect SSE
    const msgs = branch.messages || [];
    const assistantMsg = msgs[msgs.length - 1];
    if (!assistantMsg || assistantMsg.role !== "assistant") return;

    const controller = new AbortController();
    _branchStreams.set(bk, { controller, taskId, convId: conv.id, msgIdx, branchIdx });
    activeStreams.set(bk, { controller, taskId });
    renderConversationList();
    if (activeConvId === conv.id) {
      const parentMsg = conv.messages[msgIdx];
      if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      if (_activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === branchIdx) {
        _enterBranchMode(msgIdx, branchIdx);
      }
    }

    try {
      await _branchStreamSSE(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk);
    } catch (e) {
      if (e.name !== "AbortError") console.error("Branch reconnect error:", e);
    } finally {
      _finishBranchStream(conv, msgIdx, branchIdx, branch, bk);
    }
  } catch (e) {
    branch.activeTaskId = null;
    saveConversations(conv.id);
    if (activeConvId === conv.id) {
      const parentMsg = conv.messages?.[msgIdx];
      if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
    }
  }
}

// ── Initialize: scan all conversations for active branch tasks ──
function initBranchReconnect() {
  for (const conv of conversations) {
    for (let mi = 0; mi < conv.messages.length; mi++) {
      const msg = conv.messages[mi];
      if (!msg.branches) continue;
      for (let bi = 0; bi < msg.branches.length; bi++) {
        const branch = msg.branches[bi];
        if (branch.activeTaskId) {
          _reconnectBranchStream(conv, mi, bi, branch);
        }
      }
    }
  }
}

// ══════════════════════════════════════════
//  Create a new branch
// ══════════════════════════════════════════
function promptNewBranch(msgIdx, preTitle, selectedText, selectionRange) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg) return;
  // Branches are primarily for assistant messages, but allow user messages too
  if (msg.role === "user" && !selectedText) return;
  const title = preTitle || prompt("分支名称：");
  if (!title?.trim()) return;
  if (!msg.branches) msg.branches = [];
  const branch = {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    title: title.trim(),
    icon: _branchAutoIcon(title.trim()),
    messages: [],
  };
  if (selectedText) {
    branch.anchorText = selectedText.slice(0, 200);
    branch.parentSelection = selectedText;
  }
  msg.branches.push(branch);

  const bi = msg.branches.length - 1;
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
          <span class="branch-node-close" onclick="event.stopPropagation();branchCloseOrDelete(${msgIdx},${bi})" title="收起">✕</span>
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

  // Deferred save
  setTimeout(() => {
    saveConversations(conv.id);
    syncConversationToServer(conv);
  }, 100);
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

// ── Approve branch tool ──
function approveBranchTool(msgIdx, branchIdx, action) {
  const conv = getActiveConv();
  if (!conv) return;
  const bk = _branchKey(conv.id, msgIdx, branchIdx);
  const stream = _branchStreams.get(bk);
  if (!stream?.taskId) return;

  // External backend approval_required events are informational —
  // the CLI backend manages its own approval flow via subprocess stdin.
  // This is a best-effort attempt: won't work for external backends.
  console.warn("[Branch] approveBranchTool: external backend approvals are not fully supported in branch mode");
}

