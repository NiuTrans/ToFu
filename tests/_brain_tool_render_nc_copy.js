var runtimeScope = typeof window !== "undefined" ? window : globalThis;
/* ===== migrated source: ui/tool_rounds_rich.js ===== */
/* ui/tool_rounds_rich.js — DEFERRED rich tool-round renderers (Epic-E
 * pt_3879f00e sub-4, split out of ui/tool_rounds.js 2026-08-01).
 *
 * Contents: the conv-meta rich-render family (Project Brain board/charter/
 * feed/peer/digest/commit cards, ~40KB) + the Timer Watcher block +
 * its 1 Hz countdown ticker (~18KB). These render rounds that only exist
 * in conversations which used Project Brain / scheduler tools — never
 * needed for the first paint of an ordinary chat.
 *
 * Degradation contract: tool_rounds.js's _renderUnifiedToolLine dispatches
 * to _renderConvMetaBlock / _renderTimerWatcherBlock through typeof guards;
 * while this module is in flight (idle prefetch ~2s) those rounds render
 * as the generic one-line summary, and the upgrade pass below re-renders
 * the active conversation once on arrival. Window-scope sibling of
 * tool_rounds.js — every symbol it calls (escapeHtml / Icon / t /
 * _isRoundConvMeta / _CONV_META_TOOLS / _TD_SVG / _rowRightControls /
 * _localizeInspectOps …) lives in the core bundle and resolves at CALL
 * time; nothing here is read at load except literals.
 */

/* ★ Project-brain / conversation-meta block — a collapsible card that renders
   the tool's full prose output (board listing, charter text, conversation
   digest, peer status) as Markdown. These tools return their real payload in
   `round.toolContent`; the previous generic renderer showed only a name +
   badge, so the user saw NOTHING of the content. This block surfaces it.

   Header: family SVG icon + label + a source chip (Board / Charter /
   Conversations / Peer) + the action badge (read/post/…). Body: the full
   `toolContent` rendered as Markdown, falling back to the meta snippet when
   toolContent hasn't landed yet (e.g. mid-stream before tool_complete). */
/* ★ Per-tool DISPLAY metadata for the collapsed conv-meta header. The backend
   display string (`round.query`) is an English, LLM-oriented label ("Live peer
   status", "Read the project board"); on a non-English UI it reads as raw
   jargon, and — the user's core complaint — it never says WHY the tool ran or
   WHAT the result means. `_convMetaHeadLabel` returns a localized title and
   `_convMetaPurpose` a one-line plain-language caption explaining the tool's
   role in the shared "conversations-as-a-team" coordination surface (the
   Project Brain). Both are i18n keys with an English fallback for the jsdom
   harness; unknown tools fall back to the raw display string. */
function _convMetaHeadLabel(round, tFn) {
  const _t = (typeof tFn === "function") ? tFn : (k, d) => d;
  const tn = round.toolName || "";
  const raw = round.query || tn;
  const M = {
    project_board_read: ["brainHead.boardRead", "Checked the team board"],
    project_charter_read: ["brainHead.charterRead", "Read the project charter"],
    project_charter_propose: ["brainHead.charterPropose", "Proposed a charter decision"],
    project_peer_status: ["brainHead.peerStatus", "Checked who else is working now"],
    project_feed_read: ["brainHead.feedRead", "Reviewed recent team activity"],
    project_message: ["brainHead.message", "Sent a note to another conversation"],
    project_intervene: ["brainHead.intervene", "Flagged an overlap to another conversation"],
    list_conversations: ["brainHead.listConvs", "Searched past conversations"],
    get_conversation: ["brainHead.getConv", "Opened a past conversation"],
    project_claim_path: ["brainHead.claimPath", "Reserved files for editing"],
    project_release_path: ["brainHead.releasePath", "Released a file reservation"],
    project_commit: ["brainHead.commit", "Committed this conversation's work"],
  };
  if (tn.startsWith("project_board_") && !M[tn]) {
    return _t("brainHead.boardMutate", "Updated the team board");
  }
  const entry = M[tn];
  return entry ? _t(entry[0], entry[1]) : raw;
}
/* One-line "why this ran / what it means" caption. Keyed on tool name; empty
   string ⇒ no caption row (the structured card body already speaks for itself). */
function _convMetaPurpose(round, tFn) {
  const _t = (typeof tFn === "function") ? tFn : (k, d) => d;
  const tn = round.toolName || "";
  const P = {
    project_peer_status: ["brainWhy.peerStatus",
      "Sibling conversations of this project that are running right now — used to avoid duplicating work already in progress."],
    project_board_read: ["brainWhy.boardRead",
      "The shared to-do board across all conversations of this project — who is doing what, so work isn't duplicated."],
    project_feed_read: ["brainWhy.feedRead",
      "A recent timeline of what other conversations of this project have been doing."],
    project_charter_read: ["brainWhy.charterRead",
      "The project's shared goal and committed decisions that every conversation aligns to."],
    project_charter_propose: ["brainWhy.charterPropose",
      "Proposes a decision for the human to commit as shared project-wide intent — advisory until approved."],
    project_message: ["brainWhy.message",
      "An advisory note to a sibling conversation, delivered on its next turn — it never interrupts a running turn."],
    project_intervene: ["brainWhy.intervene",
      "Nudges a sibling conversation to re-check the board (advisory); a hard stop needs explicit human approval."],
    project_claim_path: ["brainWhy.claimPath",
      "Reserves specific files/paths on the shared board so sibling conversations hold off editing them while this conversation works — a durational, auto-expiring advisory lease, not a hard lock."],
    project_release_path: ["brainWhy.releasePath",
      "Clears a previously-held file/path reservation so sibling conversations may edit those paths again."],
    project_commit: ["brainWhy.commit",
      "Commits ONLY the files this conversation provably authored (byte-identical to its own last edit); files also carrying a sibling's uncommitted changes are held back, never swept in."],
    get_conversation: ["brainWhy.getConv",
      "Opens the full transcript of another past conversation — its messages, tool calls, and results — so the agent can reuse decisions or context from earlier work."],
    list_conversations: ["brainWhy.listConvs",
      "Searches your other conversations by title and content to find a relevant past discussion to reference."],
  };
  if (tn.startsWith("project_board_") && !P[tn]) {
    return _t("brainWhy.boardMutate",
      "Updates the shared to-do board so sibling conversations see this claim / change.");
  }
  const entry = P[tn];
  return entry ? _t(entry[0], entry[1]) : "";
}
/* ── Structured per-tool renderers (Phase 3) ──────────────────────────
   Each renders off the STRUCTURED meta the backend attaches (boardSnapshot /
   boardTransition / peerStatus / charterProposal) — NOT re-parsed prose. They
   return an inner-HTML string (the body of the convmeta card), or '' to fall
   back to the generic Markdown dump. */

/** Conversation digest for get_conversation: a clean, scannable transcript
 *  card (title + preset + message count meta row, then one row per message with
 *  a role chip, text preview, and tool/attachment hints) — the HUMAN view that
 *  replaces the raw `═══` / `── User Message #` ASCII dump. The verbatim
 *  transcript the model read stays available via the row's "model view" button. */
function _renderConvDigest(cd) {
  if (!cd) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const roleLabel = (r) => {
    if (r === "user") return _t("convDigest.roleUser", "User");
    if (r === "assistant") return _t("convDigest.roleAssistant", "Assistant");
    if (r === "system") return _t("convDigest.roleSystem", "System");
    return escapeHtml(r || "");
  };
  // ── Meta row: preset + message count + last-updated time. ──
  const metaBits = [];
  if (cd.preset) {
    metaBits.push(`<span class="ptool-convdigest-preset">${escapeHtml(cd.preset)}</span>`);
  }
  const nMsg = (cd.msgCount != null) ? cd.msgCount : (cd.messages || []).length;
  metaBits.push(`<span class="ptool-convdigest-msgcount">${escapeHtml(
    _t("convDigest.msgCount", "{n} messages").replace("{n}", nMsg))}</span>`);
  const updRel = _convMetaRelTime(cd.updatedAt);
  if (updRel) {
    metaBits.push(`<span class="ptool-convdigest-time" title="${escapeHtml(
      _convMetaAbsTime(cd.updatedAt))}">${escapeHtml(
      _t("convDigest.updated", "updated {t}").replace("{t}", updRel))}</span>`);
  }
  // ── RAW/debug badge: only for a get_conversation(raw=true) read. Marks the
  //    card as the debug view (per-message low-level metadata chips below) so
  //    a raw read is visibly RICHER than a normal read — inline SVG per §3.4
  //    (no emoji/glyph). A `rev` is appended when present. ──
  const isRaw = !!cd.raw;
  if (isRaw) {
    const revTxt = (cd.rev != null)
      ? " · " + _t("convDigest.rev", "rev") + " " + cd.rev : "";
    const bugSvg = '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:block"><path d="m8 2 1.88 1.88"/><path d="M14.12 3.88 16 2"/><path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/><path d="M12 20v-9"/><path d="M6.53 9C4.6 8.8 3 7.1 3 5"/><path d="M6 13H2"/><path d="M3 21c0-2.1 1.7-3.9 3.8-4"/><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"/><path d="M22 13h-4"/><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"/></svg>';
    metaBits.push(`<span class="ptool-convdigest-rawbadge icon-box" title="${escapeHtml(
      _t("convDigest.rawTip", "Raw debug read — shows per-message low-level metadata (model, tokens, finish reason, id)."))}">` +
      bugSvg + `<span class="ptool-convdigest-rawbadge-lbl">${escapeHtml(
        _t("convDigest.raw", "RAW · debug"))}${escapeHtml(revTxt)}</span></span>`);
  }
  let html = `<div class="ptool-convdigest">` +
    `<div class="ptool-convdigest-meta">${metaBits.join("")}</div>` +
    `<div class="ptool-convdigest-msgs">`;
  const msgs = cd.messages || [];
  if (!msgs.length) {
    html += `<div class="ptool-convdigest-empty">${escapeHtml(
      _t("convDigest.empty", "This conversation has no messages."))}</div>`;
  }
  for (const m of msgs) {
    // Omission marker row (head/tail seam).
    if (m && m.omitted != null) {
      html += `<div class="ptool-convdigest-omitted">${escapeHtml(
        _t("convDigest.omitted", "… {n} messages omitted …").replace("{n}", m.omitted))}</div>`;
      continue;
    }
    const roleCls = (m.role === "user" || m.role === "assistant" || m.role === "system")
      ? m.role : "other";
    const hints = [];
    if (Array.isArray(m.tools) && m.tools.length) {
      const shown = m.tools.slice(0, 6);
      const chips = shown.map(function (tl) {
        // Tools may be a rich descriptor {name, arg, status} (new) or a bare
        // string (legacy). Render name + primary arg, with a failed-status cue.
        const isObj = tl && typeof tl === "object";
        const name = isObj ? (tl.name || "") : String(tl || "");
        const arg = isObj ? (tl.arg || "") : "";
        const st = isObj ? (tl.status || "") : "";
        const failed = /error|fail|reject|abort/i.test(st);
        return `<span class="ptool-convdigest-tool${failed ? " ptool-convdigest-tool-failed" : ""}">` +
          `<span class="ptool-convdigest-tool-name">${escapeHtml(name)}</span>` +
          (arg ? `<span class="ptool-convdigest-tool-arg">${escapeHtml(arg)}</span>` : "") +
          `</span>`;
      }).join("");
      hints.push(`<span class="ptool-convdigest-tools">${(typeof Icon === "function") ? Icon("wrench", 10) : ""}` +
        chips + `${m.tools.length > 6 ? `<span class="ptool-convdigest-tool-more">+${m.tools.length - 6}</span>` : ""}</span>`);
    }
    if (m.images) {
      hints.push(`<span class="ptool-convdigest-att">${escapeHtml(
        _t("convDigest.images", "{n} image").replace("{n}", m.images))}</span>`);
    }
    if (m.pdfs) {
      hints.push(`<span class="ptool-convdigest-att">${escapeHtml(
        _t("convDigest.pdfs", "{n} PDF").replace("{n}", m.pdfs))}</span>`);
    }
    // ── RAW-mode per-message metadata chips (model / tokens / finishReason /
    //    msgId). Rendered ONLY when the digest is a raw read AND the field is
    //    present — a few compact chips, never the whole message. This is the
    //    visible difference between a raw and a normal card. ──
    if (isRaw) {
      if (m.model) {
        hints.push(`<span class="ptool-convdigest-metachip ptool-convdigest-meta-model" title="${escapeHtml(
          _t("convDigest.metaModel", "model"))}">${escapeHtml(String(m.model))}</span>`);
      }
      if (m.usage && (m.usage.in != null || m.usage.out != null)) {
        const inT = (m.usage.in != null) ? m.usage.in : "?";
        const outT = (m.usage.out != null) ? m.usage.out : "?";
        hints.push(`<span class="ptool-convdigest-metachip ptool-convdigest-meta-tok" title="${escapeHtml(
          _t("convDigest.metaTokens", "tokens in/out"))}">${escapeHtml(
          "tok " + inT + "/" + outT)}</span>`);
      }
      if (m.finishReason) {
        hints.push(`<span class="ptool-convdigest-metachip ptool-convdigest-meta-fr" title="${escapeHtml(
          _t("convDigest.metaFinish", "finish reason"))}">${escapeHtml(String(m.finishReason))}</span>`);
      }
      if (m.msgId) {
        hints.push(`<span class="ptool-convdigest-metachip ptool-convdigest-meta-id" title="${escapeHtml(
          _t("convDigest.metaId", "message id"))}">${escapeHtml(String(m.msgId))}</span>`);
      }
    }
    const text = (m.text || "").trim();
    // Per-message expand: when a capped `full` text exists and differs from the
    // preview, render a <details> so the user can open THIS message in place
    // instead of jumping to the model view.
    const full = (typeof m.full === "string") ? m.full.trim() : "";
    // `textFallback` marks a row whose text is a thinking/tool SUMMARY (the
    // message's own content was empty — a tool-only round), so we style it as
    // a muted summary with a label, never passing it off as real prose.
    const isFallback = !!m.textFallback;
    const fallbackCls = isFallback ? " ptool-convdigest-summary" : "";
    const fallbackTag = isFallback
      ? `<span class="ptool-convdigest-summary-tag">${escapeHtml(
        _t("convDigest.summary", "summary"))}</span>`
      : "";
    let textHtml;
    if (text && full && full !== text) {
      textHtml = `<details class="ptool-convdigest-expand">` +
        `<summary class="ptool-convdigest-text${fallbackCls}">${fallbackTag}${escapeHtml(text)}` +
        `<span class="ptool-convdigest-expand-hint">${escapeHtml(
          _t("convDigest.expand", "expand"))}</span></summary>` +
        `<div class="ptool-convdigest-full">${escapeHtml(full)}</div></details>`;
    } else {
      textHtml = text
        ? `<div class="ptool-convdigest-text${fallbackCls}">${fallbackTag}${escapeHtml(text)}</div>`
        : (hints.length ? "" : `<div class="ptool-convdigest-text ptool-convdigest-notext">${escapeHtml(
          _t("convDigest.noText", "(no text)"))}</div>`);
    }
    const msgRel = _convMetaRelTime(m.ts);
    const idxHtml = `<span class="ptool-convdigest-idx">#${escapeHtml(String(m.index || ""))}</span>`;
    const tsHtml = msgRel
      ? `<span class="ptool-convdigest-msgtime" title="${escapeHtml(
        _convMetaAbsTime(m.ts))}">${escapeHtml(msgRel)}</span>`
      : "";
    html += `<div class="ptool-convdigest-msg ptool-convdigest-${escapeHtml(roleCls)}">` +
      `<div class="ptool-convdigest-gutter">` +
      `<span class="ptool-convdigest-role">${escapeHtml(roleLabel(m.role))}</span>` +
      idxHtml + `</div>` +
      `<div class="ptool-convdigest-msgbody">${textHtml}` +
      ((hints.length || tsHtml) ? `<div class="ptool-convdigest-hints">${hints.join("")}${tsHtml}</div>` : "") +
      `</div></div>`;
  }
  html += `</div>`;
  if (cd.truncated && !(cd.omitted > 0)) {
    // Fallback marker when a truncation happened without an inline seam.
    html += `<div class="ptool-convdigest-more">${escapeHtml(
      _t("convDigest.truncated", "… earlier messages omitted — use the </> button on a tool row for the full request record."))}</div>`;
  }
  html += `</div>`;
  return html;
}

/* Absolute-time formatter (locale string) for the digest tooltips. */
function _convMetaAbsTime(ts) {
  const n = Number(ts) || 0;
  if (!n) return "";
  try {
    return new Date(n).toLocaleString();
  } catch (e) {
    return String(n);
  }
}

/** Mini-kanban for project_board_read: counts + per-lane epic titles. */
function _renderBoardSnapshot(snap) {
  if (!snap) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const lanes = snap.lanes || {};
  const laneDef = [
    ["open", _t("projectBrain.laneOpen", "Open")],
    ["claimed", _t("projectBrain.laneClaimed", "In progress")],
    ["done", _t("projectBrain.laneDone", "Done")],
  ];
  let html = '<div class="ptool-board-mini">';
  for (const [key, label] of laneDef) {
    const epics = lanes[key] || [];
    const count = (snap[key] != null) ? snap[key] : epics.length;
    let cards = epics.map(function (e) {
      const owner = e.owner
        ? `<span class="ptool-board-mini-owner">${escapeHtml(String(e.owner).slice(0, 8))}</span>` : "";
      const disp = e.dispatched
        ? `<span class="ptool-board-mini-auto" title="${escapeHtml(_t("projectBrain.dispatchedTitle", "Started autonomously by the project brain"))}">${(typeof Icon === "function") ? Icon("rocket", 10) : ""}</span>` : "";
      return `<div class="ptool-board-mini-card ptool-board-mini-${escapeHtml(key)}"><span class="ptool-board-mini-title">${escapeHtml(e.title || e.id || "")}</span>${owner}${disp}</div>`;
    }).join("");
    if (!cards) cards = '<div class="ptool-board-mini-empty">—</div>';
    html += `<div class="ptool-board-mini-lane"><div class="ptool-board-mini-head">${escapeHtml(label)} <span class="ptool-board-mini-count">${count}</span></div>${cards}</div>`;
  }
  html += "</div>";
  return html;
}

/* Un-escape the backend's minimal XML/HTML escaping (&amp; &lt; &gt;) so a
 * title stored as `rebuttal:&lt;venue&gt;` renders as `rebuttal:<venue>`
 * instead of showing the literal entities. Safe to pipe into _tpInlineMd,
 * which re-escapes for XSS before applying inline emphasis. */
function _unescapeEntities(s) {
  return String(s == null ? "" : s)
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

/** Explicit transition line for a board mutation (verb + epic + new status).
 *  The epic title is rendered as light inline Markdown (bold/italic/code) with
 *  entities un-escaped, so `**x**` and `<venue>` display correctly. The epic
 *  TITLE is the whole point of this card ("what was posted/claimed/…"), so it
 *  gets its own prominent row; the short epic id is a monospace traceability
 *  chip. When the backend couldn't resolve a title we degrade to a labelled
 *  placeholder rather than rendering a bare verb badge with nothing after it
 *  (the reported "shows nothing" card). */
function _renderBoardTransition(tr) {
  if (!tr || !tr.verb) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  // A mutation can FAIL by returning an error (board full, already-claimed,
  // task-not-found). The backend now carries `ok:false` + `error` so we render
  // an explicit failed card instead of a green "posted → open" that lies about
  // what happened (the reported bug: no visible failure, only in the raw text).
  const failed = tr.ok === false;
  const verbLabel = _t("projectBrain.boardVerb." + tr.verb, tr.verb);
  const rawTitle = (tr.title || "").trim();
  const titleHtml = rawTitle
    ? ((typeof _tpInlineMd === "function")
        ? _tpInlineMd(_unescapeEntities(rawTitle))
        : escapeHtml(rawTitle))
    : `<span class="ptool-board-tr-untitled">${escapeHtml(
        _t("projectBrain.boardUntitled", "(untitled epic)"))}</span>`;
  const idChip = tr.taskId
    ? `<span class="ptool-board-tr-id" title="${escapeHtml(tr.taskId)}">${escapeHtml(tr.taskId)}</span>`
    : "";
  const statusLabel = tr.status
    ? `<span class="ptool-board-tr-status ptool-board-mini-${escapeHtml(tr.status)}">${escapeHtml(_t("projectBrain.lane" + tr.status.charAt(0).toUpperCase() + tr.status.slice(1), tr.status))}</span>`
    : "";
  // On failure the "→ status" chip is replaced by a FAILED badge; the error
  // message gets its own prominent row so the user sees WHY without opening
  // the raw model text.
  const failBadge = failed
    ? `<span class="ptool-board-tr-failed">${(typeof Icon === "function") ? Icon("alertTriangle", 12) : ""}<span>${escapeHtml(_t("projectBrain.boardFailed", "failed"))}</span></span>`
    : "";
  const headRow = `<div class="ptool-board-tr-head">` +
    `<span class="ptool-board-tr-verb">${escapeHtml(verbLabel)}</span>` +
    (failed
      ? failBadge
      : (tr.status ? `<span class="ptool-board-tr-arrow">${(typeof Icon === "function") ? Icon("chevronDown", 12) : "→"}</span>${statusLabel}` : "")) +
    `</div>`;
  const titleRow = `<div class="ptool-board-tr-titlerow">` +
    `<span class="ptool-board-tr-title">${titleHtml}</span>${idChip}` +
    `</div>`;
  const errRow = (failed && (tr.error || "").trim())
    ? `<div class="ptool-board-tr-error">${escapeHtml((tr.error || "").trim())}</div>`
    : "";
  const cls = failed ? "ptool-board-transition ptool-board-transition-failed" : "ptool-board-transition";
  return `<div class="${cls}">${headRow}${titleRow}${errRow}</div>`;
}

/* Localize the small known set of backend statusLabel tokens ("generating" /
   "working" / "idle" and "editing X" / "working (phase)") so the peer card
   reads in the UI language; unknown labels pass through verbatim. */
function _localizePeerStatusLabel(sl, tFn) {
  const _t = (typeof tFn === "function") ? tFn : (k, d) => d;
  const s = String(sl || "").trim();
  if (!s) return "";
  if (s === "generating") return _t("projectBrain.stGenerating", "generating");
  if (s === "working") return _t("projectBrain.stWorking", "working");
  if (s === "idle") return _t("projectBrain.stIdle", "idle");
  let m;
  if ((m = s.match(/^editing\s+(.+)$/)))
    return _t("projectBrain.peerEditing", "editing {file}").replace("{file}", m[1]);
  if ((m = s.match(/^working\s+\((.+)\)$/)))
    return _t("projectBrain.stWorkingPhase", "working ({phase})").replace("{phase}", m[1]);
  return s;
}

/** Live peer cards for project_peer_status: conv id + status + round + epic. */
function _renderPeerStatus(ps) {
  if (!ps) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const peers = ps.peers || [];
  if (!peers.length) {
    return `<div class="ptool-peer-empty">${escapeHtml(_t("projectBrain.peerNone", "No active peers right now."))}</div>`;
  }
  let html = '<div class="ptool-peer-list">';
  for (const p of peers) {
    const who = p.title || ("conv " + String(p.convId || "").slice(0, 8));
    const sub = p.agentId ? `<span class="ptool-peer-agent">${escapeHtml("sub-agent " + p.agentId)}</span>` : "";
    const bits = [];
    if (p.statusLabel) bits.push(escapeHtml(_localizePeerStatusLabel(p.statusLabel, _t)));
    if (p.round) bits.push(_t("projectBrain.peerRound", "round {n}").replace("{n}", p.round));
    if (p.currentFile) bits.push(escapeHtml(p.currentFile));
    const epic = p.claimedEpic
      ? `<div class="ptool-peer-epic">${(typeof Icon === "function") ? Icon("package", 11) : ""}<span>${escapeHtml(p.claimedEpic)}</span></div>` : "";
    html += `<div class="ptool-peer-card">` +
      `<div class="ptool-peer-who">${(typeof Icon === "function") ? Icon("messageCircle", 12) : ""}<span>${escapeHtml(who)}</span>${sub}</div>` +
      (bits.length ? `<div class="ptool-peer-detail">${bits.join(" · ")}</div>` : "") +
      epic + `</div>`;
  }
  html += "</div>";
  return html;
}

/** Charter proposal card: the proposed text + a "pending human review" affordance. */
function _renderCharterProposal(cp) {
  if (!cp || !cp.proposal) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const titleLine = cp.title
    ? `<div class="ptool-charter-prop-title">${escapeHtml(cp.title)}</div>` : "";
  return `<div class="ptool-charter-proposal">` +
    titleLine +
    `<div class="ptool-charter-prop-text">${escapeHtml(cp.proposal)}</div>` +
    `<div class="ptool-charter-prop-pending">` +
    `${(typeof Icon === "function") ? Icon("hourglass", 11) : ""}` +
    `<span>${escapeHtml(_t("projectBrain.proposalPending", "Awaiting human review — commit or reject in the Project Brain panel"))}</span>` +
    `</div></div>`;
}

/* Relative-time formatter for a ms epoch (mirrors project-brain.js `_relTime`
   so the transcript feed reads the same as the panel). */
function _convMetaRelTime(ts) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const n = Number(ts) || 0;
  if (!n) return "";
  const secs = Math.max(0, Math.floor((Date.now() - n) / 1000));
  const mins = Math.floor(secs / 60);
  if (mins < 1) return _t("projectBrain.justNow", "just now");
  if (mins < 60) return _t("projectBrain.minutesAgo", "{n}m ago").replace("{n}", mins);
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return _t("projectBrain.hoursAgo", "{n}h ago").replace("{n}", hrs);
  const days = Math.floor(hrs / 24);
  return _t("projectBrain.daysAgo", "{n}d ago").replace("{n}", days);
}

/** Chronological activity list for project_feed_read (kind chip + who + summary
    + relative time). Reuses the feed-kind i18n labels the panel uses. */
function _renderFeedActivity(fa) {
  if (!fa) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const events = fa.events || [];
  if (!events.length) {
    return `<div class="ptool-feed-empty">${escapeHtml(_t("projectBrain.activityEmpty", "No activity yet"))}</div>`;
  }
  let html = '<div class="ptool-feed-list">';
  for (const ev of events) {
    const kind = ev.kind || "note";
    const kindLabel = _t("projectBrain.kind." + kind, kind);
    // Prefer the (backend-backfilled) title; else resolve the id to a title
    // via convTitleById — a raw `conv <id>` is meaningless to the user. Only
    // fall back to a short id when nothing resolves (conversation not loaded).
    const who = ev.title
      || (ev.convId && typeof convTitleById === "function"
        ? convTitleById(ev.convId)
        : (ev.convId ? "conv " + String(ev.convId).slice(0, 8) : ""));
    const mine = ev.mine
      ? `<span class="ptool-feed-mine">${escapeHtml(_t("projectBrain.thisConv", "this conversation"))}</span>` : "";
    const when = _convMetaRelTime(ev.ts);
    const summary = (ev.summary || "").trim();
    html += `<div class="ptool-feed-row ptool-feed-${escapeHtml(kind)}">` +
      `<span class="ptool-feed-kind">${escapeHtml(kindLabel)}</span>` +
      `<div class="ptool-feed-body">` +
      `<div class="ptool-feed-head">` +
      (who ? `<span class="ptool-feed-who">${escapeHtml(who)}</span>` : "") + mine +
      (when ? `<span class="ptool-feed-when">${escapeHtml(when)}</span>` : "") +
      `</div>` +
      (summary ? `<div class="ptool-feed-summary">${escapeHtml(summary)}</div>` : "") +
      `</div></div>`;
  }
  html += "</div>";
  return html;
}

/** Delivery card for project_message / project_intervene: the target conv, the
    message body, and a delivery-outcome chip (delivered / rate-limited / denied). */
function _renderPeerDelivery(pd) {
  if (!pd || !pd.toConv) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const isIntervene = pd.tool === "project_intervene";
  const verb = isIntervene
    ? (pd.hardAbort ? _t("projectBrain.pdHardIntervene", "Hard intervention") : _t("projectBrain.pdIntervene", "Advisory intervention"))
    : _t("projectBrain.pdMessage", "Message");
  const outcomeLabel = _t("projectBrain.pdOutcome." + (pd.outcome || "delivered"),
    pd.outcome || "delivered");
  const arrow = (typeof Icon === "function") ? Icon("chevronDown", 12) : "→";
  const body = (pd.text || "").trim();
  // Show the TARGET conversation by its human-readable title, not a raw id
  // (a bare `conv mradmzmd` is meaningless to the user). Falls back to a
  // localized "Untitled chat" via convTitleById; a short id still resolves by
  // unique prefix against the loaded conversation list.
  const _target = (typeof convTitleById === "function" && pd.toConv)
    ? convTitleById(pd.toConv)
    : ("conv " + String(pd.toConv || "").slice(0, 8));
  return `<div class="ptool-peermsg ptool-peermsg-${escapeHtml(pd.outcome || "delivered")}">` +
    `<div class="ptool-peermsg-head">` +
    `<span class="ptool-peermsg-verb">${escapeHtml(verb)}</span>` +
    `<span class="ptool-peermsg-arrow">${arrow}</span>` +
    `<span class="ptool-peermsg-target" title="${escapeHtml(String(pd.toConv || ""))}">${escapeHtml(_target)}</span>` +
    `<span class="ptool-peermsg-outcome ptool-peermsg-outcome-${escapeHtml(pd.outcome || "delivered")}">${escapeHtml(outcomeLabel)}</span>` +
    `</div>` +
    (body ? `<div class="ptool-peermsg-text">${escapeHtml(body)}</div>` : "") +
    `</div>`;
}

/** Commit result card for project_commit: the mode (planned vs committed), the
    files that were / would be committed, the files held back with the reason
    each was excluded, and the resulting sha + verify state. Renders off the
    STRUCTURED meta.commitResult the backend attaches — never re-parsed prose. */
function _renderCommitResult(cr) {
  if (!cr) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const isPlan = cr.mode === "plan";
  const committed = cr.committed || [];
  const clean = cr.clean || [];
  const excluded = cr.excluded || [];
  // The "positive" set: committed files (real commit) or would-commit files (plan).
  const posFiles = (!isPlan && cr.ok) ? committed : clean;

  // ── Outcome chip: the single most important signal (committed / planned /
  //    failed / verify-mismatch). ──
  let outcome, outClass;
  if (!cr.ok) {
    outcome = _t("commitCard.outFailed", "not committed");
    outClass = "failed";
  } else if (isPlan) {
    outcome = _t("commitCard.outPlanned", "plan only");
    outClass = "planned";
  } else if (cr.verified === false) {
    outcome = _t("commitCard.outVerifyMismatch", "verify mismatch");
    outClass = "warn";
  } else {
    outcome = _t("commitCard.outCommitted", "committed");
    outClass = "committed";
  }
  const shaHtml = (cr.commitSha && !isPlan && cr.ok)
    ? `<span class="ptool-commit-sha" title="${escapeHtml(_t("commitCard.shaTitle", "commit hash"))}">${escapeHtml(String(cr.commitSha).slice(0, 12))}</span>`
    : "";

  let html = `<div class="ptool-commit ptool-commit-${escapeHtml(outClass)}">`;
  html += `<div class="ptool-commit-head">` +
    `<span class="ptool-commit-outcome ptool-commit-outcome-${escapeHtml(outClass)}">${escapeHtml(outcome)}</span>` +
    shaHtml + `</div>`;

  if (!cr.ok && cr.error) {
    html += `<div class="ptool-commit-err">${escapeHtml(cr.error)}</div>`;
  }

  // ── The clean / committed set. ──
  if (posFiles.length) {
    const headKey = (!isPlan && cr.ok) ? "commitCard.committedHead" : "commitCard.wouldCommitHead";
    const headDef = (!isPlan && cr.ok)
      ? "Committed ({n}) — provably yours"
      : "Would commit ({n}) — provably yours";
    html += `<div class="ptool-commit-group">` +
      `<div class="ptool-commit-grouphead ptool-commit-grouphead-clean">` +
      `${(typeof Icon === "function") ? Icon("check", 11) : ""}` +
      `<span>${escapeHtml(_t(headKey, headDef).replace("{n}", posFiles.length))}</span></div>` +
      `<div class="ptool-commit-files">` +
      posFiles.map((p) => `<div class="ptool-commit-file">${escapeHtml(p)}</div>`).join("") +
      `</div></div>`;
  } else if (cr.ok) {
    html += `<div class="ptool-commit-empty">${escapeHtml(_t("commitCard.noneClean", "No files were provably attributable to this conversation."))}</div>`;
  }

  // ── The held-back / excluded set (each with its reason). ──
  if (excluded.length) {
    html += `<div class="ptool-commit-group">` +
      `<div class="ptool-commit-grouphead ptool-commit-grouphead-held">` +
      `${(typeof Icon === "function") ? Icon("hourglass", 11) : ""}` +
      `<span>${escapeHtml(_t("commitCard.heldHead", "Held back ({n}) — not committed").replace("{n}", excluded.length))}</span></div>` +
      `<div class="ptool-commit-files">` +
      excluded.map(function (e) {
        const ns = e.numstat
          ? `<span class="ptool-commit-numstat">${escapeHtml(e.numstat)}</span>` : "";
        const reason = e.reason
          ? `<span class="ptool-commit-reason">${escapeHtml(e.reason)}</span>` : "";
        return `<div class="ptool-commit-file ptool-commit-file-held">` +
          `<span class="ptool-commit-fpath">${escapeHtml(e.path || "")}</span>${ns}${reason}</div>`;
      }).join("") +
      `</div></div>`;
  }

  html += "</div>";
  return html;
}

/* ★ Default-open policy for conv-meta cards. ROUTINE COORDINATION READS
   (peer_status / board_read / feed_read / charter_read / list_conversations /
   get_conversation) the agent fires constantly and that usually need no user
   action are default-COLLAPSED — the localized header + one-line purpose
   caption stay in the summary, the multi-row body tucks away until clicked, so
   the transcript isn't dominated by low-signal noise. MUTATING / DECISION cards
   (project_message / project_intervene / project_charter_propose / board
   mutations) represent an action the agent TOOK and stay OPEN. */
const _CONV_META_ROUTINE_READS = new Set([
  "project_peer_status", "project_board_read", "project_feed_read",
  "project_charter_read", "list_conversations",
]);
function _convMetaDefaultOpen(round) {
  const tn = round.toolName || "";
  // Board MUTATIONS (post/claim/complete/block) are actions → open.
  if (tn.startsWith("project_board_") && tn !== "project_board_read") return true;
  // get_conversation is the PRIMARY viewing product of the "View Conversation"
  // tool — its digest card is the main deliverable, so it stays OPEN (default
  // expanded) rather than hiding the transcript behind a click.
  return !_CONV_META_ROUTINE_READS.has(tn);
}

/* At-a-glance count chip for a COLLAPSED routine-read summary, so the user sees
   "3 peers active" / "5 open" without expanding. Empty ⇒ no chip. Driven off
   the same structured meta the body renders (never re-parsed prose). */
function _convMetaSummaryChip(round, meta, tFn) {
  const _t = (typeof tFn === "function") ? tFn : (k, d) => d;
  const tn = round.toolName || "";
  let n = null, label = "";
  if (tn === "project_peer_status" && meta.peerStatus) {
    n = (meta.peerStatus.peers || []).length;
    label = _t("brainChip.peers", "{n} active").replace("{n}", n);
  } else if (tn === "project_board_read" && meta.boardSnapshot) {
    const snap = meta.boardSnapshot;
    n = (snap.open != null) ? snap.open : (snap.lanes && snap.lanes.open ? snap.lanes.open.length : 0);
    label = _t("brainChip.openEpics", "{n} open").replace("{n}", n);
  } else if (tn === "project_feed_read" && meta.feedActivity) {
    n = (meta.feedActivity.events || []).length;
    label = _t("brainChip.events", "{n} events").replace("{n}", n);
  } else if (tn === "get_conversation" && meta.convDigest) {
    n = (meta.convDigest.msgCount != null)
      ? meta.convDigest.msgCount : (meta.convDigest.messages || []).length;
    label = _t("brainChip.msgs", "{n} messages").replace("{n}", n);
  } else {
    return "";
  }
  if (n == null) return "";
  return `<span class="ptool-convmeta-count">${escapeHtml(label)}</span>`;
}

/** Pick the structured body for a conv-meta round, or '' to fall back. */
function _structuredConvMetaBody(round, meta) {
  if (meta.boardSnapshot) return _renderBoardSnapshot(meta.boardSnapshot);
  if (meta.boardTransition) return _renderBoardTransition(meta.boardTransition);
  if (meta.peerStatus) return _renderPeerStatus(meta.peerStatus);
  if (false) return _renderFeedActivity(meta.feedActivity);
  if (meta.peerDelivery) return _renderPeerDelivery(meta.peerDelivery);
  if (meta.charterProposal) return _renderCharterProposal(meta.charterProposal);
  if (meta.commitResult) return _renderCommitResult(meta.commitResult);
  if (meta.convDigest) return _renderConvDigest(meta.convDigest);
  return "";
}

/* Localized source chip label (Board / Charter / Conversations / Peer). The
   backend `meta.source` is an English family tag; translate it for the chip. */
const _CONV_META_SOURCE_I18N = {
  Board: ["brainSrc.board", "Team board"],
  Charter: ["brainSrc.charter", "Charter"],
  Conversations: ["brainSrc.conversations", "Conversations"],
  ConvRef: ["brainSrc.conversations", "Conversations"],
  Peer: ["brainSrc.peer", "Team"],
  Git: ["brainSrc.git", "Git"],
};

function _renderConvMetaBlock(round, svg, q, badgeHtml) {
  const meta = (round.results || [])[0] || {};
  const _t = (typeof t === "function") ? t : (k, d) => d;
  // project_commit is routed through the Board handler (source==="Board") but
  // it is a git operation, not a board action — give it its own source chip.
  const source = (round.toolName === "project_commit") ? "Git" : (meta.source || "");
  const srcEntry = _CONV_META_SOURCE_I18N[source];
  const sourceLabel = srcEntry ? _t(srcEntry[0], srcEntry[1]) : (source || "");
  const sourceChip = sourceLabel
    ? `<span class="ptool-convmeta-src">${escapeHtml(sourceLabel)}</span>`
    : "";
  // ★ Localized, plain-language header + a "why this ran / what it means"
  //   caption. Replaces the raw English backend display string (round.query)
  //   that the user found meaningless.
  const headLabel = _convMetaHeadLabel(round, _t);
  const purpose = _convMetaPurpose(round, _t);
  const purposeHtml = purpose
    ? `<div class="ptool-convmeta-why">${escapeHtml(purpose)}</div>`
    : "";
  // ★ Structured renderer first (driven off backend meta, not re-parsed prose).
  //   When a structured body is available it replaces the raw Markdown dump;
  //   otherwise we fall back to the full toolContent (charter_read text,
  //   conversation digests, peer message/intervene results, etc.).
  const structured = _structuredConvMetaBody(round, meta);
  let bodyHtml;
  if (structured) {
    bodyHtml = `<div class="ptool-convmeta-structured">${structured}</div>`;
  } else {
    // Full content preferred; snippet is the mid-stream / pre-complete fallback.
    const content = (typeof round.toolContent === "string" && round.toolContent.trim())
      ? round.toolContent
      : (typeof meta.snippet === "string" ? meta.snippet : "");
    bodyHtml = content.trim()
      ? `<div class="ptool-convmeta-content md-content">${renderMarkdown(content)}</div>`
      : `<div class="ptool-convmeta-empty">${escapeHtml(_t("tool.noContent", "No content returned."))}</div>`;
  }
  // ★ Default-collapse routine coordination READS (low-signal, fired
  //   constantly); keep MUTATING / DECISION cards open (they show an action
  //   the agent took). A collapsed read still carries an at-a-glance count
  //   chip in its summary ("3 peers active" / "5 open") so the user gets the
  //   signal without expanding.
  const isOpen = _convMetaDefaultOpen(round);
  const openAttr = isOpen ? " open" : "";
  const countChip = isOpen ? "" : _convMetaSummaryChip(round, meta, _t);
  return `<details class="ptool-convmeta-block"${openAttr} data-rn="${round.roundNum}">
       <summary class="ptool-line ptool-convmeta-header">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${escapeHtml(headLabel)}</span>
         ${countChip}
         ${sourceChip}
         ${badgeHtml}
       </summary>
       <div class="ptool-convmeta-body">${purposeHtml}${bodyHtml}</div>
     </details>`;
}

/* ── Timer Watcher Block ──
   Renders the timer_create tool call as a collapsible panel showing
   each poll check (wait/ready/error) with timestamps and reasons.
   While polling, shows a live "watching…" header; after trigger, shows "✓ triggered". */
/* Countdown text for the "Next check in Ns" hint. Kept in one place so the
 * initial render and the 1 Hz ticker below produce identical strings. */
function _timerNextPollText(nextTs) {
  const _tf = (typeof t === "function") ? t : (k, d) => d;
  const secs = Math.max(0, Math.round((nextTs - Date.now()) / 1000));
  return secs > 0
    ? _tf("timerBlock.nextCheckIn", "Next check in ~{n}s…").replace("{n}", secs)
    : _tf("timerBlock.nextCheckNow", "Next check due now…");
}

/* Turn a raw backend poll `reason` into a plain, translated verdict for the
 * poll line. The code/hybrid reconcile primitive (lib/scheduler/_shared.py)
 * emits developer-English notes like "predicate no match (exit=1)" /
 * "predicate matched (exit=0)" that leaked verbatim into the (otherwise
 * localized) timer card. Recognize those shapes and render a human, i18n'd
 * verdict; leave a genuine LLM/free-form reason untouched. Returns the string
 * to display (already NOT html-escaped — caller escapes). */
function _timerPollReasonText(p, _t) {
  const raw = p && p.reason ? String(p.reason) : "";
  // "predicate no match (exit=1)" / "predicate matched (exit=0)" — the pure
  // code/predicate verdict. Map to a plain ready/not-ready line + exit code.
  const m = raw.match(/^predicate (matched|no match) \(exit=(-?\d+)\)$/);
  if (m) {
    const isMatch = m[1] === "matched";
    const code = m[2];
    return isMatch
      ? _t("timerBlock.predicateReady", "Condition met (command exit {code})").replace("{code}", code)
      : _t("timerBlock.predicateWait", "Not met yet (command exit {code})").replace("{code}", code);
  }
  if (/^predicate ambiguous/.test(raw)) {
    return _t("timerBlock.predicateAmbiguous", "Command result inconclusive — still waiting");
  }
  return raw;
}

function _renderTimerWatcherBlock(round, svg) {
  const polls = round._timerPolls || [];
  const isActive = round.status === "searching";
  const triggered = round._timerTriggered;
  const timerId = round._timerTimerId || "";
  const totalPolls = polls.filter(p => p.decision !== "started").length;
  const timerIdShort = timerId ? timerId.slice(0, 12) : "";
  // Was the most recent poll a parse/LLM error? Surface it in the header so
  // a stuck verification (LLM not returning a usable decision) is obvious.
  const realPolls = polls.filter(p => p.decision !== "started");
  const lastPoll = realPolls.length ? realPolls[realPolls.length - 1] : null;
  const lastWasError = lastPoll && (lastPoll.decision === "error" || lastPoll.decision === "parse_error" || lastPoll.parseError);
  // Condition tier (backend `condition_kind`): a pure-code timer decides by a
  // shell predicate and NEVER calls an LLM — so it gets a distinct identity and
  // no "Verifier model" row. LLM/hybrid timers resolve a cheap model AT EACH
  // POLL, and that model can differ per poll (it shows on each poll line), so
  // we deliberately don't pin a single model in the header/meta.
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const condKind = round._timerConditionKind
    || (round._timerCheckInstruction ? "llm"
        : (round._timerConditionCommand ? "code" : "llm"));
  const isCodeTimer = condKind === "code";

  // Header — the timer id is surfaced as a dedicated copyable chip (rendered
  // separately below), so the label text no longer embeds the raw id.
  let headerLabel, headerCls;
  const _idTxt = escapeHtml(timerIdShort);
  const _s = (n) => (n !== 1 ? "s" : "");
  if (triggered) {
    headerLabel = _t("timerBlock.headTriggered", "Timer — triggered after {n} poll{s}")
      .replace("{n}", totalPolls).replace("{s}", _s(totalPolls));
    headerCls = "timer-watcher-triggered";
  } else if (round._timerOrphaned) {
    headerLabel = _t("timerBlock.headOrphaned", "Timer — task interrupted ({n} poll{s}, timer still active in background)")
      .replace("{n}", totalPolls).replace("{s}", _s(totalPolls));
    headerCls = "timer-watcher-orphaned";
  } else if (isActive) {
    const skipN = round._timerSkipCount || 0;
    const skipSuffix = skipN > 0 ? _t("timerBlock.headSkipSuffix", ", {n} skipped").replace("{n}", skipN) : "";
    const errSuffix = lastWasError ? _t("timerBlock.headErrSuffix", ", last check errored") : "";
    headerLabel = _t("timerBlock.headWatching", "Timer — watching… ({n} poll{s}{skip}{err})")
      .replace("{n}", totalPolls).replace("{s}", _s(totalPolls))
      .replace("{skip}", skipSuffix).replace("{err}", errSuffix);
    headerCls = lastWasError ? "timer-watcher-active timer-watcher-warn" : "timer-watcher-active";
  } else {
    headerLabel = _t("timerBlock.headDone", "Timer — {status} ({n} poll{s})")
      .replace("{status}", escapeHtml(round.status || "done"))
      .replace("{n}", totalPolls).replace("{s}", _s(totalPolls));
    headerCls = "";
  }
  // Dedicated copyable id chip — clicking it copies the FULL timer id to the
  // clipboard (handled by document delegation on `.timer-id-chip`), so the
  // long identifier is extracted out of the label into one prominent token.
  const idChip = timerId
    ? `<button class="timer-id-chip" data-timer-id="${escapeHtml(timerId)}" title="${escapeHtml(_t("timerBlock.idChipTitle", "Timer id — click to copy"))}"><span class="timer-id-txt">${_idTxt}</span>${Icon("clipboard", 10)}</button>`
    : "";
  // Distinct identity chip: a pure command-based (zero-LLM) timer vs a hybrid.
  const kindBadge = isCodeTimer
    ? `<span class="timer-kind-badge timer-kind-code" title="${escapeHtml(_t("timerBlock.kindCodeTip", "Decided by a shell command — no model is called"))}">${escapeHtml(_t("timerBlock.kindCode", "command-based"))}</span>`
    : (condKind === "hybrid"
        ? `<span class="timer-kind-badge timer-kind-hybrid" title="${escapeHtml(_t("timerBlock.kindHybridTip", "Model decides; a shell command runs alongside and takes over once it consistently agrees"))}">${escapeHtml(_t("timerBlock.kindHybrid", "hybrid"))}</span>`
        : "");

  // ── What is being verified — show the check instruction + command so the
  //    user understands the timer's job, who runs it, and how often. ──
  //    The instruction can be long; render it expandable instead of clipping
  //    mid-sentence (the old slice(0,400) cut "report st…").
  let metaHtml = "";
  const instr = round._timerCheckInstruction || "";
  const cmd = round._timerCheckCommand || round._timerConditionCommand || "";
  const interval = round._timerPollInterval || 0;
  const maxPolls = round._timerMaxPolls || 0;
  if (instr || cmd || interval) {
    const cadence = interval
      ? (maxPolls
          ? _t("timerBlock.cadenceMax", "Checks every {n}s · up to {m} times").replace("{n}", interval).replace("{m}", maxPolls)
          : _t("timerBlock.cadence", "Checks every {n}s").replace("{n}", interval))
      : "";

    // Expandable instruction, rendered as Markdown (the model writes it in
    // Markdown). A collapsed max-height clamp reveals the full text on click —
    // the backend now ships the whole instruction, so "show more" shows all.
    let instrHtml = "";
    if (instr) {
      const LONG = instr.length > 160;
      const valId = "tw-instr-" + round.roundNum;
      const bodyHtml = (typeof renderMarkdown === "function")
        ? renderMarkdown(instr)
        : escapeHtml(instr);
      const moreTxt = _t("timerBlock.showMore", "show more");
      const lessTxt = _t("timerBlock.showLess", "show less");
      const toggle = LONG
        ? ` data-tofu-action="event.stopPropagation();var v=document.getElementById('${valId}');v.classList.toggle('expanded');this.querySelector('.timer-meta-more').textContent=v.classList.contains('expanded')?this.querySelector('.timer-meta-more').getAttribute('data-less'):this.querySelector('.timer-meta-more').getAttribute('data-more');"`
        : "";
      const moreLink = LONG
        ? `<span class="timer-meta-more" data-more="${escapeHtml(moreTxt)}" data-less="${escapeHtml(lessTxt)}">${escapeHtml(moreTxt)}</span>` : "";
      instrHtml = `<div class="timer-meta-row timer-meta-row-instr"${toggle}>
        <span class="timer-meta-label">${escapeHtml(_t("timerBlock.verifying", "Verifying"))}</span>
        <span class="timer-meta-val timer-meta-md md-content${LONG ? " timer-meta-clamp" : ""}" id="${valId}">${bodyHtml}</span>
        ${moreLink}
      </div>`;
    }

    // Who decides the trigger. For a pure command-based (code) timer there is
    // NO model — the shell predicate decides. For LLM/hybrid the deciding model
    // is resolved AT EACH POLL and can differ between polls, so we describe the
    // tier here and let each poll line carry its own model chip (no misleading
    // single pinned model in the header).
    const deciderRow = isCodeTimer
      ? `<div class="timer-meta-row"><span class="timer-meta-label">${escapeHtml(_t("timerBlock.decidedBy", "Decided by"))}</span><span class="timer-meta-val">${escapeHtml(_t("timerBlock.deciderCode", "Shell command exit code — no model"))}</span></div>`
      : `<div class="timer-meta-row"><span class="timer-meta-label">${escapeHtml(_t("timerBlock.verifier", "Verifier"))}</span><span class="timer-meta-val">${escapeHtml(_t("timerBlock.verifierLLM", "Cheap LLM · resolved per poll (see each check below)"))}</span></div>`;

    const cmdLabel = isCodeTimer
      ? _t("timerBlock.predicate", "Predicate")
      : _t("timerBlock.command", "Command");

    metaHtml = `<div class="timer-watcher-meta">
      ${instrHtml}
      ${cmd ? `<div class="timer-meta-row"><span class="timer-meta-label">${escapeHtml(cmdLabel)}</span><code class="timer-meta-cmd">${escapeHtml(cmd.slice(0, 300))}</code></div>` : ""}
      ${deciderRow}
      ${cadence ? `<div class="timer-meta-row"><span class="timer-meta-label">${escapeHtml(_t("timerBlock.cadenceLabel", "Cadence"))}</span><span class="timer-meta-val">${escapeHtml(cadence)}</span></div>` : ""}
    </div>`;
  }

  // ── "Next check in Ns" hint while active ──
  // The countdown text is refreshed in place every second by the 1 Hz
  // ticker at the bottom of this module (keyed on [data-timer-next]), so it
  // stays live without churning the fingerprint gate — mirrors the swarm
  // panel's [data-sw-start] approach.
  let nextPollHtml = "";
  if (isActive && round._timerNextPollTs) {
    nextPollHtml = `<div class="timer-next-poll" data-timer-next="${round._timerNextPollTs}">${Icon('hourglass', 12)} <span class="timer-next-poll-txt">${_timerNextPollText(round._timerNextPollTs)}</span></div>`;
  }

  // Build poll lines (most recent first for readability)
  const reversed = [...polls].reverse();
  const MAX_VISIBLE = 5;
  const visible = reversed.slice(0, MAX_VISIBLE);
  const hidden = reversed.length - MAX_VISIBLE;

  let pollLines = "";
  for (const p of visible) {
    let icon, cls, label;
    const isParseErr = p.decision === "parse_error" || p.parseError;
    if (p.decision === "started") {
      icon = Icon('bell', 13); cls = "timer-poll-started"; label = "";
    } else if (p.decision === "ready") {
      icon = Icon('save', 13); cls = "timer-poll-ready"; label = `#${p.pollNum}`;
    } else if (p.decision === "error") {
      icon = Icon('ban', 13); cls = "timer-poll-error"; label = `#${p.pollNum}`;
    } else if (isParseErr) {
      icon = Icon('zap', 13); cls = "timer-poll-error timer-poll-parse-err"; label = `#${p.pollNum}`;
    } else {
      icon = Icon('hourglass', 13); cls = "timer-poll-wait"; label = `#${p.pollNum}`;
    }
    const ts = p.ts ? new Date(p.ts).toLocaleTimeString() : "";
    // Plain, translated verdict for the visible line — a raw predicate note
    // ("predicate no match (exit=1)") becomes "Not met yet (command exit 1)".
    // A genuine LLM reason passes through unchanged.
    const fullReason = _timerPollReasonText(p, _t);
    const reason = escapeHtml(fullReason.slice(0, 120));
    const tokens = p.tokensUsed ? ` · ${p.tokensUsed} tok` : "";
    // The raw LLM output — only meaningful (and only sent/persisted) when the
    // decision could not be parsed. This is the evidence that explains WHY the
    // poll failed, so it is the centerpiece of an errored poll's detail.
    const rawContent = (isParseErr && p.rawContent) ? String(p.rawContent) : "";
    // Per-poll model chip — which LLM made this decision.
    const modelChip = p.model
      ? `<span class="timer-poll-model" title="${escapeHtml(_t("timerBlock.verifiedByTitle", "Verified by {model}").replace("{model}", p.model))}">${escapeHtml(p.model)}</span>` : "";
    // Stable per-poll id (e.g. tmr_84bd4fb3.p36) — lets the user correlate this
    // exact check with the app.log line and the DB poll_log row.
    const pollIdChip = p.pollId
      ? `<span class="timer-poll-id" title="${escapeHtml(_t("timerBlock.pollIdTitle", "Poll id — search app.log for this"))}">${escapeHtml(p.pollId)}</span>` : "";
    // Per-poll tool-call timeline — reuse the swarm panel's .sw-tl-* look so
    // the timer's tool activity reads identically to a sub-agent's.
    const trace = Array.isArray(p.toolTrace) ? p.toolTrace : [];

    // Expandable detail: full reason + raw LLM output + tool-call timeline + check_command output.
    const hasDetail = (fullReason.length > 120) || rawContent.length > 0 || trace.length > 0 || (p.cmdOutput && p.cmdOutput.length > 0);
    let detailHtml = "";
    if (hasDetail) {
      const fullReasonHtml = fullReason.length > 120
        ? `<div class="timer-poll-detail-reason">${escapeHtml(fullReason)}</div>` : "";
      // Raw LLM output — what the model actually returned when its decision
      // could not be parsed as JSON. Shown verbatim so the failure is diagnosable.
      const rawHtml = rawContent.length > 0
        ? `<div class="timer-poll-detail-label">${escapeHtml(_t("timerBlock.rawOutput", "Raw LLM output (unparseable decision):"))}</div>` +
          `<pre class="timer-poll-detail-output timer-poll-raw"><code>${escapeHtml(rawContent)}</code></pre>`
        : "";
      let traceHtml = "";
      if (trace.length > 0) {
        const rows = trace.map(tc => {
          const td = (typeof _TOOL_DISPLAY !== "undefined") ? _TOOL_DISPLAY[tc.name] : null;
          const ticon = (td && td.icon) ? td.icon : _TD_SVG('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>');
          const dot = tc.isError
            ? `<span class="sw-tl-dot sw-tl-failed">✕</span>`
            : `<span class="sw-tl-dot sw-tl-done">✓</span>`;
          const el = (typeof tc.elapsed === "number") ? `${tc.elapsed.toFixed(1)}s` : "";
          return `<div class="sw-tl-row sw-tl-${tc.isError ? "failed" : "done"}">
            <div class="sw-tl-line">${dot}<span class="sw-tl-icon">${ticon}</span>` +
            `<span class="sw-tl-name">${escapeHtml(tc.name || "?")}</span>` +
            (tc.argsBrief ? `<span class="sw-tl-args" title="${escapeHtml(tc.argsBrief)}">${escapeHtml(tc.argsBrief)}</span>` : "") +
            (el ? `<span class="sw-tl-elapsed">${el}</span>` : "") +
          `</div></div>`;
        }).join("");
        traceHtml = `<div class="timer-poll-detail-label">${escapeHtml(_t("timerBlock.toolsCalled", "Tools called this poll:"))}</div>` +
          `<div class="sw-a-timeline timer-poll-trace">${rows}</div>`;
      }
      const cmdOutHtml = (p.cmdOutput && p.cmdOutput.length > 0)
        ? `<div class="timer-poll-detail-label">${escapeHtml(_t("timerBlock.checkOutput", "Check output (evidence):"))}</div><pre class="timer-poll-detail-output"><code>${escapeHtml(p.cmdOutput)}</code></pre>`
        : "";
      detailHtml = `<div class="timer-poll-detail">${fullReasonHtml}${rawHtml}${traceHtml}${cmdOutHtml}</div>`;
    }
    const toggleAttr = hasDetail
      ? ` data-tofu-action="event.stopPropagation();var d=this.nextElementSibling;if(d){d.classList.toggle('expanded');this.classList.toggle('expanded');}"`
      : "";
    const caret = hasDetail ? `<span class="timer-poll-caret">▸</span>` : `<span class="timer-poll-caret-spacer"></span>`;
    const toolBadge = trace.length > 0
      ? `<span class="timer-poll-toolcount" title="${escapeHtml(_t("timerBlock.toolCallsTitle", "{n} tool call(s) this poll").replace("{n}", trace.length))}">${Icon('wrench', 11)} ${trace.length}</span>` : "";
    pollLines += `<div class="timer-poll-line ${cls}${hasDetail ? " timer-poll-has-detail" : ""}"${toggleAttr}>
      ${caret}
      <span class="timer-poll-icon">${icon}</span>
      <span class="timer-poll-num">${label}</span>
      <span class="timer-poll-reason">${reason}</span>
      ${toolBadge}${pollIdChip}${modelChip}
      <span class="timer-poll-meta">${ts}${tokens}</span>
    </div>${detailHtml}`;
  }

  let hiddenHtml = "";
  if (hidden > 0) {
    hiddenHtml = `<div class="timer-poll-hidden">${escapeHtml(_t("timerBlock.hiddenChecks", "{n} earlier check{s} hidden").replace("{n}", hidden).replace("{s}", hidden !== 1 ? "s" : ""))}</div>`;
  }

  // ★ Skip heartbeat trailer — shows "N polls skipped (output unchanged)"
  //   so the user knows the timer is still alive even when the LLM isn't
  //   being called. Without this, long runs of identical check_command
  //   output look like the timer is frozen.
  let skipTrailer = "";
  if (round._timerSkipCount && isActive) {
    const skipTs = round._timerLastSkipTs
      ? new Date(round._timerLastSkipTs).toLocaleTimeString()
      : "";
    const lastPollNum = round._timerLastSkipPollNum || 0;
    skipTrailer = `<div class="timer-poll-line timer-poll-skipped">
      <span class="timer-poll-icon">${Icon('clock', 13)}</span>
      <span class="timer-poll-num">${lastPollNum ? `#${lastPollNum}` : ""}</span>
      <span class="timer-poll-reason">${escapeHtml(_t("timerBlock.skipped", "{n} poll{s} skipped — check_command output unchanged").replace("{n}", round._timerSkipCount).replace("{s}", round._timerSkipCount !== 1 ? "s" : ""))}</span>
      <span class="timer-poll-meta">${skipTs}</span>
    </div>`;
  }

  const uid = "tmr-r" + round.roundNum;
  const expandedByDefault = isActive;  // auto-expand while active
  return `<div class="timer-watcher-block ${headerCls}" data-rn="${round.roundNum}">
       <div class="timer-watcher-header" data-tofu-action="if(event.target.closest('.timer-id-chip,.ri-tool-anchor'))return;event.stopPropagation();var w=document.getElementById('${uid}-wrap');w.classList.toggle('expanded');var t=this.querySelector('.timer-toggle');if(t)t.textContent=w.classList.contains('expanded')?'▾':'▸';">
         <span class="timer-watcher-icon icon-box">${Icon('timer', 13)}</span>
         ${idChip}
         <span class="timer-watcher-label">${headerLabel}</span>
         ${kindBadge}
         ${isActive ? '<span class="ptool-spinner"></span>' : ''}
         ${_rowRightControls(round)}
         <span class="timer-toggle">${expandedByDefault ? '▾' : '▸'}</span>
       </div>
       <div class="timer-watcher-body${expandedByDefault ? ' expanded' : ''}" id="${uid}-wrap">
         ${metaHtml}${pollLines}${hiddenHtml}${skipTrailer}${nextPollHtml}
       </div>
     </div>`;
}

/* ── 1 Hz wall-clock ticker for the timer "Next check in Ns" countdown ──
 * Like the swarm panel's elapsed timers, the countdown text changes every
 * second even when no SSE event landed. The fingerprint gate in
 * _syncToolRoundsDOM (correctly) skips re-renders when nothing changed, so
 * without this the hint froze at whatever value it was first painted with.
 * We update [data-timer-next] elements in place: zero re-render, single
 * timer, O(N active timers) per tick — mirrors _tickSwarmTimers. */
function _tickTimerCountdowns() {
  const els = document.querySelectorAll('.timer-next-poll[data-timer-next]');
  if (!els.length) return;
  for (const el of els) {
    const nextTs = +el.getAttribute('data-timer-next');
    if (!nextTs) continue;
    const span = el.querySelector('.timer-next-poll-txt');
    if (!span) continue;
    const txt = _timerNextPollText(nextTs);
    if (span.textContent !== txt) span.textContent = txt;
  }
}
if (typeof window !== 'undefined' && !runtimeScope._timerCountdownTicker) {
  runtimeScope._timerCountdownTicker = setInterval(() => {
    try {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
      _tickTimerCountdowns();
    } catch (e) { /* swallowed — countdown ticker is best-effort */ }
  }, 1000);
}

/* Upgrade pass: a conv rendered while this module was in flight got the
 * generic-line degradation for conv-meta / timer-watcher rounds. Re-render
 * the ACTIVE conversation once so they upgrade to their rich cards.
 * Skipped while a stream is live on the conv (the stream re-renders itself
 * with the rich renderer now present), and a no-op when no such rounds
 * exist (the common case — the lazy ESM domain idle-prefetches every boot). */
(function _upgradeDegradedToolRounds() {
  try {
    if (typeof getActiveConv !== 'function') return;
    const conv = getActiveConv();
    if (!conv || !conv.messages) return;
    if (typeof activeStreams !== 'undefined' && activeStreams && activeStreams.has(conv.id)) return;
    const hasRich = conv.messages.some((m) => m && Array.isArray(m.toolRounds) && m.toolRounds.some((r) =>
      r && ((r._timerPolls && r._timerPolls.length) || r._timerSkipCount ||
        (typeof _isRoundConvMeta === 'function' && _isRoundConvMeta(r)) ||
        (typeof _isRoundMotion === 'function' && _isRoundMotion(r)))));
    /* Full-tree contract (test_full_repaints_route_through_replaceAll): every
     * whole-conv repaint routes through the ConvView seam — bare renderChat(
     * is globally zero outside conv_view.js / chat_render.js. forceScroll:
     * false because this is a silent background upgrade — never yank the
     * user's scroll position for it. */
    if (hasRich) runtimeScope.ConvView.replaceAll(conv.id, { forceScroll: false });
  } catch (e) { /* best-effort upgrade — the next natural render fixes it */ }
})();

/* ══════════════════════════════════════════════════════════════════════════
 * Motion-video / produce tool cards
 *
 * The owner screenshot (2026-08-06): a motion_video_check / motion_video_render
 * round rendered as a bare fn-name + a badge — no idea WHAT the call did or
 * what came back. These tools return rich structured JSON envelopes
 * (probe specs, per-gate findings, per-scene narration durations, mux
 * verification, background-job handles); this block renders them as a
 * collapsible card instead of hiding everything behind the badge.
 * ══════════════════════════════════════════════════════════════════════════ */

/* Per-tool localized header + one-line plain-language purpose caption.
 * The backend `round.query` ("Render scene-001 → scene-001.mp4 (draft)") is
 * kept as a detail row in the body; the header is the user-facing verb. */
const _MOTION_HEAD_I18N = {
  motion_video_env_check:        ["motionHead.envCheck", "Checked the video toolchain"],
  motion_video_storyboard_check: ["motionHead.storyboard", "Validated the storyboard against its transcript"],
  motion_video_check:            ["motionHead.check", "Ran the static quality gates on a scene"],
  motion_video_render:           ["motionHead.render", "Rendered a scene to video"],
  motion_video_probe:            ["motionHead.probe", "Inspected a media file"],
  motion_video_concat:           ["motionHead.concat", "Assembled the scenes into the final video"],
  motion_video_narrate:          ["motionHead.narrate", "Synthesized the narration track"],
  motion_video_mux:              ["motionHead.mux", "Merged narration into the video"],
  produce_video:                 ["motionHead.produceVideo", "Started a video job"],
  produce_report:                ["motionHead.produceReport", "Started a research report"],
  produce_research:              ["motionHead.produceResearch", "Started a research survey"],
  produce_slides:                ["motionHead.produceSlides", "Started a slide-deck job"],
  edit_slides:                   ["motionHead.editSlides", "Edited one page of a deck"],
};
const _MOTION_PURPOSE_I18N = {
  motion_video_env_check:        ["motionWhy.envCheck", "Confirms Node / HyperFrames / ffmpeg / headless Chrome are all present before any render work starts."],
  motion_video_storyboard_check: ["motionWhy.storyboard", "Zero-LLM gate: the storyboard must cover the narration transcript end-to-end before any scene may render."],
  motion_video_check:            ["motionWhy.check", "Lint + headless-Chrome runtime check + layout inspection. A scene must pass before it is allowed to render."],
  motion_video_render:           ["motionWhy.render", "Renders one scene composition to MP4 via headless Chrome (deterministic)."],
  motion_video_probe:            ["motionWhy.probe", "Reads codec / resolution / fps / duration / audio-track from a media file — the post-render spec check."],
  motion_video_concat:           ["motionWhy.concat", "Joins the per-scene MP4s into one final video; mismatched scenes are normalized to the first scene's spec."],
  motion_video_narrate:          ["motionWhy.narrate", "Synthesizes one narration WAV per scene and reports the duration each scene must become (alignment manifest)."],
  motion_video_mux:              ["motionWhy.mux", "Copies the video stream and merges the narration track as normalized AAC — the deliverable file."],
  produce_video:                 ["motionWhy.produceVideo", "One sentence → finished narrated video. The job runs in the background; progress shows in the video panel."],
  produce_report:                ["motionWhy.produceReport", "One sentence → a cited long-form report, published as a markdown artifact when it finishes."],
  produce_research:              ["motionWhy.produceResearch", "Harvests the recent literature, then proposes and scores genuinely-novel research ideas against it."],
  produce_slides:                ["motionWhy.produceSlides", "One sentence → a designer-quality editable PPTX: scenario theme + bound palette/typefaces + per-page layout + visual QA. Downloads when done."],
  edit_slides:                   ["motionWhy.editSlides", "Re-authors ONE page of a produced deck from your plain-language instruction, then re-renders the preview and re-exports the PPTX."],
};

/* The motion handler's meta.title is the bare stage name ("check" / "render")
 * — noisy inside the card, so the motion body never renders a snippet. The
 * produce handlers' note / quality_hint DO carry user-meaningful text. */
function _motionMetaSnippet(round, meta) {
  const tn = round.toolName || "";
  if (tn.startsWith("motion_video_")) return "";
  return (typeof meta.snippet === "string") ? meta.snippet : "";
}

function _motionEsc(s) {
  return escapeHtml(String(s == null ? "" : s));
}

function _motionDetailRow(label, value, mono) {
  if (value == null || value === "") return "";
  const cls = mono ? "ptool-motion-v ptool-motion-mono" : "ptool-motion-v";
  return `<div class="ptool-motion-row"><span class="ptool-motion-k">${_motionEsc(label)}</span><span class="${cls}">${_motionEsc(value)}</span></div>`;
}

function _motionSection(title, innerHtml) {
  if (!innerHtml) return "";
  return `<div class="ptool-motion-section"><div class="ptool-motion-section-title">${_motionEsc(title)}</div>${innerHtml}</div>`;
}

function _motionList(items, cls, max) {
  if (!Array.isArray(items) || !items.length) return "";
  const cap = max || 12;
  const shown = items.slice(0, cap);
  const rows = shown.map((it) => `<div class="ptool-motion-li ${cls || ""}">${_motionEsc(it)}</div>`).join("");
  const more = items.length > cap
    ? `<div class="ptool-motion-li ptool-motion-more">… +${items.length - cap} more</div>` : "";
  return rows + more;
}

/* Parse the handler's JSON envelope out of round.toolContent (a pretty-printed
 * JSON string). Returns {} on any non-JSON shape — the card then falls back
 * to the meta snippet. */
function _motionPayload(round) {
  const raw = round.toolContent;
  if (typeof raw !== "string" || !raw.trim()) return {};
  try { const d = JSON.parse(raw); return (d && typeof d === "object") ? d : {}; }
  catch (e) { return {}; }
}

/* ── Per-tool structured bodies ─────────────────────────────────────── */

function _motionBodyEnvCheck(p, _t) {
  const ok = !!p.ok;
  const statusLine = _motionDetailRow(
    _t("motionRow.status", "Status"),
    ok ? _t("motionRow.envReady", "ready — every dependency resolved") : _t("motionRow.envMissing", "missing dependencies"),
  );
  const deps = [];
  const depRow = (name, val) => {
    const present = typeof val === "string" ? !!val : !!val;
    deps.push(`<div class="ptool-motion-dep ${present ? "ptool-motion-dep-ok" : "ptool-motion-dep-miss"}">`
      + `<span class="ptool-motion-dep-name">${_motionEsc(name)}</span>`
      + `<span class="ptool-motion-dep-val">${present ? _motionEsc(typeof val === "string" ? val : "ok") : _t("motionRow.absent", "not found")}</span></div>`);
  };
  depRow("node", p.node);
  depRow("hyperframes", p.hyperframes);
  depRow("ffmpeg", p.ffmpeg);
  depRow("ffprobe", p.ffprobe);
  depRow("chrome", p.chrome);
  const issues = _motionList(p.issues, "ptool-motion-warn");
  return statusLine + _motionSection(_t("motionSec.deps", "Dependencies"), deps.join(""))
       + _motionSection(_t("motionSec.issues", "Issues"), issues);
}

function _motionBodyStoryboard(p, _t) {
  const span = Array.isArray(p.span_s) && p.span_s.length === 2
    ? `${p.span_s[0]}s → ${p.span_s[1]}s` : "";
  const rows = _motionDetailRow(_t("motionRow.transcriptSpan", "Transcript span"), span, true)
    + _motionDetailRow(_t("motionRow.errors", "Errors"), Array.isArray(p.errors) ? String(p.errors.length) : "0");
  const errs = _motionList(p.errors, "ptool-motion-err");
  return rows + _motionSection(_t("motionSec.problems", "Problems to fix"), errs);
}

function _motionBodyCheck(p, _t) {
  const ok = !!p.ok;
  const gates = p.gates && typeof p.gates === "object" ? Object.keys(p.gates) : [];
  let rows = _motionDetailRow(_t("motionRow.verdict", "Verdict"),
    ok ? _t("motionRow.gatesPass", "all gates passed") : _t("motionRow.gatesFail", "gate failed"), false);
  if (!ok && p.category) rows += _motionDetailRow(_t("motionRow.category", "Failure kind"), p.category, true);
  if (p.elapsed != null) rows += _motionDetailRow(_t("motionRow.elapsed", "Elapsed"), `${p.elapsed}s`, true);
  if (gates.length) rows += _motionDetailRow(_t("motionRow.gates", "Gates run"), gates.join(", "));
  const errs = _motionList(p.errors, "ptool-motion-err");
  const warns = _motionList(p.warnings, "ptool-motion-warn");
  const hints = _motionList(p.fix_hints, "ptool-motion-hint");
  return rows
    + _motionSection(_t("motionSec.errors", "Errors"), errs)
    + _motionSection(_t("motionSec.warnings", "Warnings"), warns)
    + _motionSection(_t("motionSec.fixHints", "How to fix"), hints);
}

function _motionBodyRender(p, _t) {
  const ok = !!p.ok;
  let rows = "";
  if (ok) {
    rows += _motionDetailRow(_t("motionRow.output", "Output"), p.output, true);
    if (p.render_time_s != null) rows += _motionDetailRow(_t("motionRow.renderTime", "Render time"), `${p.render_time_s}s`, true);
    if (p.elapsed != null) rows += _motionDetailRow(_t("motionRow.wall", "Wall clock"), `${p.elapsed}s`, true);
  } else {
    rows += _motionDetailRow(_t("motionRow.category", "Failure kind"), p.category || "failed");
    if (p.detail) rows += `<div class="ptool-motion-li ptool-motion-err">${_motionEsc(String(p.detail).slice(0, 600))}</div>`;
  }
  return rows;
}

function _motionBodyProbe(p, _t) {
  const pr = p.probe;
  if (!pr || typeof pr !== "object" || !Object.keys(pr).length) {
    return `<div class="ptool-motion-li ptool-motion-err">${_motionEsc(_t("motionRow.probeFailed", "probe failed — not a readable media file"))}</div>`;
  }
  const res = (pr.width && pr.height) ? `${pr.width}×${pr.height}` : "";
  let rows = _motionDetailRow(_t("motionRow.codec", "Codec"), pr.codec, true)
    + _motionDetailRow(_t("motionRow.resolution", "Resolution"), res, true)
    + _motionDetailRow(_t("motionRow.fps", "Frame rate"), pr.fps != null ? `${pr.fps} fps` : "", true)
    + _motionDetailRow(_t("motionRow.duration", "Duration"), pr.duration != null ? `${Number(pr.duration).toFixed(2)}s` : "", true)
    + _motionDetailRow(_t("motionRow.audioTrack", "Audio track"),
        pr.has_audio ? _t("motionRow.audioYes", "present") : _t("motionRow.audioNo", "silent"));
  return rows;
}

function _motionBodyConcat(p, _t) {
  if (!p.ok) {
    return _motionDetailRow(_t("motionRow.category", "Failure kind"), p.category || "failed")
      + (p.detail ? `<div class="ptool-motion-li ptool-motion-err">${_motionEsc(String(p.detail).slice(0, 600))}</div>` : "");
  }
  return _motionDetailRow(_t("motionRow.output", "Output"), p.output, true)
    + _motionDetailRow(_t("motionRow.duration", "Duration"), p.duration != null ? `${p.duration}s` : "", true);
}

function _motionBodyNarrate(p, _t) {
  if (p.degraded) {
    return `<div class="ptool-motion-li ptool-motion-warn">${_motionEsc(
      _t("motionRow.narrateDegraded", "No TTS slot configured — the video ships silent."))}</div>`
      + (p.detail ? `<div class="ptool-motion-li">${_motionEsc(p.detail)}</div>` : "");
  }
  const scenes = Array.isArray(p.scenes) ? p.scenes : [];
  const head = _motionDetailRow(_t("motionRow.scenes", "Scenes"), String(scenes.length))
    + _motionDetailRow(_t("motionRow.alignment", "Alignment"), p.alignment, true)
    + (p.overflow_total ? _motionDetailRow(_t("motionRow.overflow", "Overflow"), `${p.overflow_total}s`, true) : "");
  if (!scenes.length) return head;
  const rows = scenes.map((s) => {
    const audio = s.audio_duration != null ? `${Number(s.audio_duration).toFixed(2)}s` : "—";
    const target = s.target_duration != null ? `${Number(s.target_duration).toFixed(2)}s` : "—";
    const grew = (s.target_duration != null && s.srt_duration != null
      && Number(s.target_duration) > Number(s.srt_duration) + 0.001);
    return `<div class="ptool-motion-scene${grew ? " ptool-motion-scene-grew" : ""}">`
      + `<span class="ptool-motion-scene-id">${_motionEsc(s.scene_id)}</span>`
      + `<span class="ptool-motion-scene-dur">${_t("motionRow.sceneDur", "audio {a} → scene {t}").replace("{a}", audio).replace("{t}", target)}</span>`
      + (s.overflow ? `<span class="ptool-motion-scene-overflow">+${s.overflow}s</span>` : "")
      + `</div>`;
  }).join("");
  return head + _motionSection(_t("motionSec.sceneDurations", "Per-scene durations"), rows);
}

function _motionBodyMux(p, _t) {
  if (!p.ok) {
    return _motionDetailRow(_t("motionRow.category", "Failure kind"), p.category || "failed")
      + (p.detail ? `<div class="ptool-motion-li ptool-motion-err">${_motionEsc(String(p.detail).slice(0, 600))}</div>` : "");
  }
  return _motionDetailRow(_t("motionRow.deliverable", "Deliverable"), p.output, true)
    + _motionDetailRow(_t("motionRow.duration", "Duration"), p.duration != null ? `${p.duration}s` : "", true)
    + _motionDetailRow(_t("motionRow.audioTrack", "Audio track"), _t("motionRow.audioVerified", "verified present"));
}

function _motionBodyProduce(p, _t) {
  if (!p.ok) {
    return `<div class="ptool-motion-li ptool-motion-err">${_motionEsc(p.detail || _t("motionRow.jobFailed", "job failed to start"))}</div>`;
  }
  let rows = "";
  if (p.task_id) rows += _motionDetailRow(_t("motionRow.taskId", "Task"), String(p.task_id).slice(0, 20), true);
  if (p.topic) rows += _motionDetailRow(_t("motionRow.topic", "Topic"), p.topic);
  if (p.direction) rows += _motionDetailRow(_t("motionRow.direction", "Direction"), p.direction);
  if (p.visual_quality) rows += _motionDetailRow(_t("motionRow.quality", "Quality"), p.visual_quality, true);
  if (p.depth) rows += _motionDetailRow(_t("motionRow.depth", "Depth"), p.depth, true);
  if (p.deduped) rows += _motionDetailRow(_t("motionRow.deduped", "Note"), _t("motionRow.dedupedText", "joined the already-running job"));
  if (p.quality_hint) rows += `<div class="ptool-motion-li">${_motionEsc(p.quality_hint)}</div>`;
  return rows;
}

const _MOTION_BODY = {
  motion_video_env_check: _motionBodyEnvCheck,
  motion_video_storyboard_check: _motionBodyStoryboard,
  motion_video_check: _motionBodyCheck,
  motion_video_render: _motionBodyRender,
  motion_video_probe: _motionBodyProbe,
  motion_video_concat: _motionBodyConcat,
  motion_video_narrate: _motionBodyNarrate,
  motion_video_mux: _motionBodyMux,
  produce_video: _motionBodyProduce,
  produce_report: _motionBodyProduce,
  produce_research: _motionBodyProduce,
};

function _renderMotionVideoBlock(round, svg, q, badgeHtml) {
  const meta = (round.results || [])[0] || {};
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const tn = round.toolName || "";
  const headEntry = _MOTION_HEAD_I18N[tn];
  const headLabel = headEntry ? _t(headEntry[0], headEntry[1]) : (round.query || tn);
  const purposeEntry = _MOTION_PURPOSE_I18N[tn];
  const purpose = purposeEntry ? _t(purposeEntry[0], purposeEntry[1]) : "";
  const purposeHtml = purpose
    ? `<div class="ptool-convmeta-why">${escapeHtml(purpose)}</div>` : "";
  const p = _motionPayload(round);
  const bodyFn = _MOTION_BODY[tn];
  let bodyInner = bodyFn ? bodyFn(p, _t) : "";
  /* The call line ("Render scene-001 → scene-001.mp4 (draft)") is kept as the
   * last detail row — it's the model-facing label, still useful verbatim. */
  const callLine = (typeof round.query === "string" && round.query && round.query !== tn)
    ? _motionDetailRow(_t("motionRow.call", "Call"), round.query, true) : "";
  const snippet = _motionMetaSnippet(round, meta);
  const snippetHtml = (!bodyInner && snippet)
    ? `<div class="ptool-motion-li">${_motionEsc(snippet)}</div>` : "";
  if (!bodyInner && !snippetHtml && !callLine) return "";
  const emptyNote = (!bodyInner && !snippetHtml)
    ? `<div class="ptool-convmeta-empty">${escapeHtml(_t("tool.noContent", "No content returned."))}</div>` : "";
  const bodyHtml = `<div class="ptool-motion-body">${purposeHtml}${bodyInner}${snippetHtml}${emptyNote}${callLine}</div>`;
  return `<details class="ptool-convmeta-block ptool-motion-block" data-rn="${round.roundNum}">
       <summary class="ptool-line ptool-convmeta-header">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${escapeHtml(headLabel)}</span>
         <span class="ptool-convmeta-src">${escapeHtml(_t("motionSrc", "Video"))}</span>
         ${badgeHtml}
       </summary>
       ${bodyHtml}
     </details>`;
}

