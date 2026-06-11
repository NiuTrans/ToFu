/* ═══════════════════════════════════════════════════════════════════
   tool rounds — extracted from ui.js (split 2026-05-28)

   Tool-round rendering: web_search / fetch_url / code_exec / project / browser / image_gen / swarm.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ★ Round type detection
function _isRoundFetch(round) {
  return (
    round.toolName === "fetch_url" ||
    (round.query || "").startsWith("📄") ||
    (round.query || "").startsWith("🌐") ||
    (round.query || "").startsWith("📑")
  );
}
function _isRoundSearch(round) {
  return round.toolName === "web_search";
}
function _isRoundCodeExec(round) {
  return round.toolName === "code_exec";
}
function _isRoundProject(round) {
  return [
    "read_files",
    "list_dir",
    "grep_search",
    "find_files",
    "write_file",
    "apply_diff",
    "apply_diffs",
    "insert_content",
    "insert_contents",
    "create_project",
    "run_command",
  ].includes(round.toolName);
}
function _isRoundBrowser(round) {
  return [
    "browser_list_tabs",
    "browser_read_tab",
    "browser_execute_js",
    "browser_screenshot",
    "browser_get_cookies",
    "browser_get_history",
    "browser_create_tab",
    "browser_close_tab",
    "browser_navigate",
  ].includes(round.toolName);
}
function _isRoundImageGen(round) {
  return round.toolName === "generate_image";
}
function _isRoundSwarm(round) {
  /* Only treat as swarm if the backend flagged it AND there's real swarm content */
  if (!round._swarm) return false;
  /* Must have at least one agent OR meaningful results to render the swarm panel.
     Even during active spawning, we don't show the panel until agents arrive. */
  if (!round._swarmAgents?.length && !round.results?.length) return false;
  return true;
}

/* ★ Tool display metadata — icon, label, color for non-search/fetch tools */
const _TOOL_DISPLAY = {
  web_search:    { icon: "", label: "Searching", color: "#60a5fa" },
  fetch_url:     { icon: "", label: "Fetching",  color: "#34d399" },
  spawn_agents:     { icon: "", label: "Swarm",          color: "#f59e0b" },
  await_agents:     { icon: "", label: "Awaiting Swarm", color: "#f59e0b" },
  get_agent_result: { icon: "", label: "Agent Result",   color: "#f59e0b" },
  create_memory:  { icon: "", label: "Memory",     color: "#a78bfa" },
  schedule_task: { icon: "", label: "Schedule",  color: "#fb923c" },
  timer_create:  { icon: "⏱️", label: "Timer Watcher", color: "#a855f7" },
  timer_manage:  { icon: "⏱️", label: "Timer",   color: "#a855f7" },
  bash_exec:     { icon: "▶️", label: "Running",   color: "#f472b6" },
  desktop_click: { icon: "", label: "Desktop",   color: "#94a3b8" },
  desktop_type:  { icon: "⌨️", label: "Desktop",   color: "#94a3b8" },
  desktop_screenshot: { icon: "", label: "Desktop", color: "#94a3b8" },
  generate_image: { icon: "", label: "Image", color: "#e879f9" },
  ask_human: { icon: "", label: "Guidance", color: "#a5b4fc" },
};
function _getToolDisplay(round) {
  if (_TOOL_DISPLAY[round.toolName]) return _TOOL_DISPLAY[round.toolName];
  if (_isRoundFetch(round))   return { icon: "", label: "Fetching",  color: "#34d399" };
  if (_isRoundSearch(round))  return { icon: "", label: "Searching", color: "#60a5fa" };
  if (_isRoundSwarm(round))   return { icon: "", label: "Swarm",     color: "#f59e0b" };
  if (_isRoundProject(round)) return { icon: "", label: "Project",   color: "#60a5fa" };
  if (_isRoundBrowser(round)) return { icon: "", label: "Browser",   color: "#38bdf8" };
  // Generic fallback — use the tool name itself
  const name = (round.toolName || "tool").replace(/_/g, " ");
  return { icon: "⚡", label: name.charAt(0).toUpperCase() + name.slice(1), color: "#94a3b8" };
}

function _getRoundBlockClass(round) {
  if (_isRoundFetch(round)) return "fetch-block";
  return "";
}
function _getRoundIcon(round) {
  if (_isRoundProject(round)) {
    const m = {
      read_files: "file",
      list_dir: "folder",
      grep_search: "search",
      find_files: "find",
      write_file: "write",
      apply_diff: "diff",
      apply_diffs: "diff",
      insert_content: "insert",
      insert_contents: "insert",
      create_project: "folder",
      run_command: "terminal",
    };
    return m[round.toolName] || "folder";
  }
  if (_isRoundBrowser(round)) {
    const m = {
      browser_list_tabs: "tabs",
      browser_read_tab: "read",
      browser_execute_js: "js",
      browser_screenshot: "screenshot",
      browser_get_cookies: "cookie",
      browser_get_history: "history",
      browser_create_tab: "newtab",
      browser_close_tab: "close",
      browser_navigate: "navigate",
    };
    return m[round.toolName] || "tabs";
  }
  // Web search / fetch / generic
  if (_isRoundSearch(round)) return "web_search";
  if (_isRoundFetch(round)) return "fetch";
  if (_isRoundCodeExec(round)) return "code_exec";
  return round.toolName || "generic";
}
function _getRoundColor(round) {
  if (_isRoundImageGen(round)) return _imageGenMode(round) === "edit" ? "#22d3ee" : "#e879f9";
  if (_isRoundProject(round)) return "#f59e0b";
  if (_isRoundBrowser(round)) return "#a78bfa";
  if (_isRoundFetch(round)) return "#34d399";
  if (_isRoundSearch(round)) return "#60a5fa";
  if (_isRoundCodeExec(round)) return "#f472b6";
  return "#94a3b8";
}

// ═══════════════════════════════════════════
//  ★ Code Execution — Inline code block (legacy, kept for compat)
// ═══════════════════════════════════════════
function _renderCodeExecBlock(round, isSearching) {
  const meta = (round.results || [])[0] || {};
  const cmd = escapeHtml(meta.command || round.query || "");
  if (isSearching) {
    return `<div class="code-exec-block code-exec-running">
         <div class="code-exec-header"><span class="code-exec-icon">⚡</span><span class="code-exec-label">Running...</span><span class="ptool-spinner"></span></div>
         <pre class="code-exec-cmd"><code>$ ${cmd}</code></pre>
       </div>`;
  }
  const exitCode = meta.exitCode ?? "?";
  const timedOut = meta.timedOut || false;
  const output = meta.output || "";
  const isOk = exitCode === "0" || exitCode === 0;
  const statusCls = timedOut
    ? "code-exec-timeout"
    : isOk
      ? "code-exec-ok"
      : "code-exec-err";
  const statusLabel = timedOut
    ? "Timeout"
    : isOk
      ? "✓ Done"
      : `✗ exit ${exitCode}`;
  const outputHtml = output
    ? `<pre class="code-exec-output"><code>${escapeHtml(output)}</code></pre>`
    : "";
  return `<div class="code-exec-block ${statusCls}">
       <div class="code-exec-header"><span class="code-exec-icon">⚡</span><span class="code-exec-label">Code Execution</span><span class="code-exec-status">${statusLabel}</span></div>
       <pre class="code-exec-cmd"><code>$ ${cmd}</code></pre>
       ${outputHtml}
     </div>`;
}

// ═══════════════════════════════════════════
//  ★ Unified Tool Activity Panel
// ═══════════════════════════════════════════
const _projToolSvg = {
  file: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  folder:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  search:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>',
  find: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/><line x1="8" y1="11" x2="14" y2="11"/></svg>',
  write:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  diff: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v18"/><path d="M8 8l4-4 4 4"/><path d="M8 16l4 4 4-4"/></svg>',
  terminal:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
};

// ── Browser Tools — SVG Icons ──
const _browserToolSvg = {
  tabs: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 3v6"/></svg>',
  read: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
  js: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  screenshot:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M3 9h2"/><path d="M19 9h2"/></svg>',
  cookie:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a10 10 0 1 0 10 10 4 4 0 0 1-5-5 4 4 0 0 1-5-5"/><circle cx="8" cy="14" r="1"/><circle cx="12" cy="18" r="1"/><circle cx="16" cy="14" r="1"/></svg>',
  history:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  newtab:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
  close:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>',
  navigate:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>',
};
// ── Web/Fetch/Generic Tools — SVG Icons ──
const _webToolSvg = {
  web_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/><circle cx="11" cy="11" r="3" stroke-dasharray="2 2"/></svg>',
  fetch: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  code_exec: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
  create_memory: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L9 9l-7 1 5 5-1 7 6-3 6 3-1-7 5-5-7-1z"/></svg>',
  update_memory: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  delete_memory: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
  merge_memories: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 3v6a6 6 0 0 0 6 6 6 6 0 0 0 6-6V3"/><line x1="12" y1="15" x2="12" y2="21"/></svg>',
  search_memories: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>',
  schedule_task: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  ask_human: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  generic: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
};

const _imageGenSvg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';
/* Image EDITING icon — a wand/sparkle to visually separate "edit an existing
 * image" from "generate from scratch" (the framed-photo icon above). */
const _imageEditSvg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>';
/* Chip-sized (12px) glyphs for the mode chips — Lucide "sparkles" (generate)
 * and "wand" (edit). Inline SVG, not emoji, per the icon convention (§3.4). */
const _imageGenChipSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/></svg>';
const _imageEditChipSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>';
/* Return 'edit' or 'generate' for an image-gen round (defaults to generate). */
function _imageGenMode(round) {
  const m = (round.results && round.results[0]) || {};
  return m.imageMode === "edit" ? "edit" : "generate";
}

/* ── Get the correct SVG for any tool type ── */
function _getToolSvg(round) {
  const icon = _getRoundIcon(round);
  if (_isRoundImageGen(round)) return _imageGenMode(round) === "edit" ? _imageEditSvg : _imageGenSvg;
  if (_isRoundProject(round)) return _projToolSvg[icon] || _projToolSvg.file;
  if (_isRoundBrowser(round)) return _browserToolSvg[icon] || _browserToolSvg.tabs;
  return _webToolSvg[icon] || _webToolSvg[round.toolName] || _webToolSvg.generic;
}

/* ── Human Guidance card — interactive Q&A card from the LLM ── */
/* ★ Auto-translate integration: when conv.autoTranslate is ON, the LLM's
 *   question & choice options are automatically translated EN→CN for display.
 *   The user's free-text reply is auto-translated CN→EN before sending.
 *   This mirrors the same auto-translate flow as regular chat messages. */
function _renderHumanGuidanceCard(round, svg) {
  const gid = escapeHtml(round.guidanceId || '');
  const rawQuestion = round.guidanceQuestion || 'The AI needs your input';
  const respType = round.guidanceType || 'free_text';
  /* ★ Defensive: guidanceOptions MUST be an array for options.map(…) below.
   *   The LLM sometimes returns null, undefined, a JSON string, or an object
   *   instead of a proper array — and older persisted conversations may
   *   contain such legacy shapes.  Without this normalization, the caller
   *   hits `TypeError: options.map is not a function` which crashes the
   *   whole toolRounds sync pass.  See routes/common client-error logs. */
  let options = round.guidanceOptions;
  if (typeof options === 'string') {
    try { options = JSON.parse(options); }
    catch (e) { options = []; }
  }
  if (!Array.isArray(options)) options = [];

  // ★ Use translated question if available (populated by _autoTranslateHumanGuidance)
  const displayQuestion = round._translatedQuestion || rawQuestion;
  const isTranslating = !!round._hgTranslating;
  // Render the question with full Markdown — same renderer as assistant messages
  const questionHtml = renderMarkdown(displayQuestion);

  // ★ Translating indicator (shown while async EN→CN translation is in-flight)
  const translatingIndicator = isTranslating
    ? `<div class="hg-translating-indicator"><span class="hg-spinner"></span> 正在翻译问题…</div>`
    : '';

  let inputHtml = '';
  if (respType === 'choice' && options.length > 0) {
    // ── Multiple-choice option cards ──
    // ★ Use translated labels/descriptions if available
    const optCardsHtml = options.map((opt, i) => {
      const origLabel = opt.label || `Option ${i + 1}`;
      const displayLabel = opt._translatedLabel || origLabel;
      const displayDesc = opt._translatedDescription || opt.description || '';
      const descHtml = displayDesc
        ? `<div class="hg-opt-desc">${renderMarkdown(displayDesc)}</div>`
        : '';
      // ★ Always send the ORIGINAL English label to backend (not the translated one)
      // ★ escapeHtml the JSON.stringify output so double-quotes don't break onclick="..." attribute
      const safeJsonLabel = escapeHtml(JSON.stringify(origLabel));
      return `<button class="hg-option-card" data-gid="${gid}" data-label="${escapeHtml(origLabel)}"
                      onclick="event.stopPropagation();submitHumanGuidanceChoice('${gid}',${safeJsonLabel})">
                <div class="hg-opt-label">${escapeHtml(displayLabel)}</div>
                ${descHtml}
              </button>`;
    }).join('');
    inputHtml = `<div class="hg-options-grid">${optCardsHtml}</div>`;
  } else {
    // ── Free-text input area ──
    // ★ No manual Translate button — auto-translate is handled automatically
    //   on submit (CN→EN) when conv.autoTranslate is ON.
    inputHtml = `<div class="hg-freetext-wrap">
      <textarea class="hg-textarea" id="hg-input-${gid}" rows="3"
                placeholder="输入你的回答（支持中文，会自动翻译）…"
                onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();submitHumanGuidanceFreeText('${gid}')}"></textarea>
      <div class="hg-freetext-actions">
        <button class="hg-submit-btn" onclick="event.stopPropagation();submitHumanGuidanceFreeText('${gid}')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          提交
        </button>
      </div>
    </div>`;
  }

  return `<div class="hg-card" data-gid="${gid}">
    <div class="hg-header">
      
      <span class="hg-title">AI 需要你的指导</span>
      <span class="hg-badge">等待回复</span>
    </div>
    ${translatingIndicator}
    <div class="hg-question">${questionHtml}</div>
    ${inputHtml}
  </div>`;
}

/* ── Multi-root: small pill that prefixes a filesystem tool line with
 *   the workspace-root name the call targets (e.g. "tofu:" / "hope-mcp:").
 *   Backend attaches `_toolRoot` to the round only when (a) the tool is a
 *   filesystem tool and (b) the workspace has more than one root.  We
 *   add an extra frontend guard so single-root sessions stay unprefixed
 *   even if a stale `_toolRoot` field arrives. */
function _renderToolRootPill(round) {
  if (!round || !round._toolRoot) return "";
  const _ps = (typeof projectState !== "undefined") ? projectState : null;
  const _extrasCount = (_ps && Array.isArray(_ps.extraRoots)) ? _ps.extraRoots.length : 0;
  if (_extrasCount === 0) return "";
  return `<span class="ptool-root" title="Workspace root">${escapeHtml(round._toolRoot)}:</span>`;
}


/**
 * Render the "auto-fixed" badge shown when the harness repaired a tool
 * call's malformed arguments before executing it (e.g. recovered truncated
 * JSON, or coerced a stringified array). `round._repaired` is
 * `{label, detail, patterns}` emitted by lib/tasks_pkg/tool_dispatch.py.
 * The tooltip explains exactly what was corrected.
 */
function _renderToolRepairedBadge(round) {
  const rep = round && round._repaired;
  if (!rep) return "";
  /* The repair changed the call's SHAPE, but that doesn't guarantee the
   * call then SUCCEEDED. When the executed tool still failed (write tools
   * set meta.writeOk === false), claiming "auto-fixed" is misleading — the
   * coercion produced a still-broken call. Downgrade to "fix attempted"
   * (amber) so the badge matches the red failure badge next to it. */
  const meta = (round.results || [])[0] || {};
  const stillFailed = meta.writeOk === false;
  const label = escapeHtml(stillFailed ? "fix attempted" : (rep.label || "auto-fixed"));
  const tip = escapeHtml(
    (stillFailed
      ? "Harness coerced this call's malformed arguments, but the call still failed"
      : "Harness auto-corrected this call's arguments before running it") +
    (rep.detail ? ":\n" + rep.detail : ".")
  );
  const cls = stillFailed ? "ptool-badge-warn" : "ptool-badge-repaired";
  return `<span class="ptool-badge ${cls}" title="${tip}">🔧 ${label}</span>`;
}

/**
 * Strip a leading "<file_token>:" prefix from an apply_diffs description
 * when that token already names the same file shown on the right.
 *
 * Models routinely write "database/_core.py: drop legacy alias" — the path
 * is already rendered as a separate column, so the prefix is pure noise
 * and forces aggressive truncation. Drop it whenever the prefix matches
 * the basename, the relative path, or any path-segment of `path`.
 */
function _stripPathPrefixFromDesc(desc, path) {
  if (!desc) return "";
  const m = String(desc).match(/^([^\s:]{1,80}):\s+(.+)$/);
  if (!m) return desc;
  const prefix = m[1];
  const rest = m[2];
  if (!path) return desc;
  const p = String(path);
  const segs = p.split("/").filter(Boolean);
  const basename = segs[segs.length - 1] || "";
  const stem = basename.replace(/\.[^./]+$/, "");
  // Match against full path, basename, basename-without-ext, or any segment.
  const candidates = new Set([p, basename, stem, ...segs]);
  if (candidates.has(prefix)) return rest;
  return desc;
}

/**
 * Render a git-style unified diff between two text blocks.
 * Uses LCS on lines to produce del/add/ctx hunks — no scrollbar, full expand.
 */
/**
 * Render a git-style unified diff between two text blocks.
 * Uses LCS on lines to produce del/add/ctx hunks — no scrollbar, full expand.
 */
function _renderLineDiff(oldText, newText) {
  // Filter out empty trailing entries from split
  const oldLines = oldText ? oldText.split("\n") : [];
  const newLines = newText ? newText.split("\n") : [];
  if (!oldLines.length && !newLines.length) return "";
  // Pure deletion or pure addition — no LCS needed
  if (!newLines.length || (newLines.length === 1 && newLines[0] === "")) {
    let h = "";
    oldLines.forEach(l => { h += `<div class="bdiff-line bdiff-del"><span class="bdiff-sign">-</span><code>${escapeHtml(l)}</code></div>`; });
    return `<div class="bdiff-block">${h}</div>`;
  }
  if (!oldLines.length || (oldLines.length === 1 && oldLines[0] === "")) {
    let h = "";
    newLines.forEach(l => { h += `<div class="bdiff-line bdiff-add"><span class="bdiff-sign">+</span><code>${escapeHtml(l)}</code></div>`; });
    return `<div class="bdiff-block">${h}</div>`;
  }
  const m = oldLines.length, n = newLines.length;
  // For very large diffs, fall back to simple before/after
  if (m + n > 300) {
    let h = "";
    oldLines.forEach(l => { h += `<div class="bdiff-line bdiff-del"><span class="bdiff-sign">-</span><code>${escapeHtml(l)}</code></div>`; });
    h += `<div class="bdiff-sep"></div>`;
    newLines.forEach(l => { h += `<div class="bdiff-line bdiff-add"><span class="bdiff-sign">+</span><code>${escapeHtml(l)}</code></div>`; });
    return `<div class="bdiff-block">${h}</div>`;
  }
  // Build LCS table
  const dp = Array.from({length: m + 1}, () => new Uint16Array(n + 1));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = oldLines[i-1] === newLines[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
  // Backtrack to produce diff ops
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i-1] === newLines[j-1]) {
      ops.push({type: "ctx", text: oldLines[i-1]});
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
      ops.push({type: "add", text: newLines[j-1]});
      j--;
    } else {
      ops.push({type: "del", text: oldLines[i-1]});
      i--;
    }
  }
  ops.reverse();
  let html = "";
  ops.forEach(op => {
    const sign = op.type === "del" ? "-" : op.type === "add" ? "+" : " ";
    html += `<div class="bdiff-line bdiff-${op.type}"><span class="bdiff-sign">${sign}</span><code>${escapeHtml(op.text)}</code></div>`;
  });
  return `<div class="bdiff-block">${html}</div>`;
}

/* ── Async-swarm inbox-injection row ──
 * Renders a synthetic toolRound (flagged `_inboxInject`) that marks the
 * moment the orchestrator drained the model's inbox and injected N
 * <swarm-update> messages as a user message before the next LLM round.
 * Collapsible: the header shows "📨 Received N sub-agent update(s)" and
 * the expanded body shows each agent id + the raw <swarm-update> payload
 * the model actually saw — so the human sees exactly what the model got. */
function _renderInboxInjectRow(round) {
  const count = round.inboxCount || (round.inboxPreviews || []).length || 0;
  const ids = (round.inboxAgentIds || []).filter(Boolean);
  const previews = Array.isArray(round.inboxPreviews) ? round.inboxPreviews : [];
  const idsLabel = ids.length
    ? `<span class="sw-inbox-row-ids">[${ids.slice(0, 4).map(escapeHtml).join(", ")}${ids.length > 4 ? ` +${ids.length - 4}` : ""}]</span>`
    : "";
  const word = count === 1 ? "update" : "updates";
  const bodyHtml = previews.length
    ? previews.map(p => {
        const aid = escapeHtml(p.agentId || "");
        const text = escapeHtml(p.text || "");
        return `<div class="sw-inbox-row-item">` +
          (aid ? `<div class="sw-inbox-row-agent">${aid}</div>` : "") +
          `<pre class="sw-inbox-row-payload">${text}</pre>` +
        `</div>`;
      }).join("")
    : `<div class="sw-inbox-row-empty">No payload preview available.</div>`;
  return `<details class="sw-inbox-row" data-rn="${round.roundNum}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon">📨</span>
         <span class="ptool-text">Received <b>${count}</b> async sub-agent ${word}</span>
         ${idsLabel}
         <span class="ptool-badge ptool-badge-info">injected → context</span>
         <span class="sw-inbox-row-chev">▾</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
}

/* ── Vertical-domain card: HF Papers / Semantic Scholar / arXiv / etc. ──
 * Distinct from web results: renders one labeled card per domain that
 * carried structured items, ranked by upvotes (HF) or citations (S2).
 * Brand icons live under static/icons/ per CLAUDE.md §3.4. */
function _renderVerticalIcon(domain) {
  const d = (domain || "").toLowerCase();
  // Generic SVG sigils per domain — keep emoji-free per project convention.
  if (d === "academic")
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.5 3 3 6 3s6-1.5 6-3v-5"/></svg>';
  if (d === "code")
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>';
  if (d === "finance")
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 17 9 11 13 15 21 7"/><polyline points="14 7 21 7 21 14"/></svg>';
  if (d === "security")
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.5 9-8 10-4.5-1-8-5-8-10V6l8-4z"/></svg>';
  if (d === "network")
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"/></svg>';
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><circle cx="11" cy="11" r="3"/></svg>';
}

function _renderVerticalCard(v) {
  if (!v || typeof v !== "object") return "";
  const items = Array.isArray(v.items) ? v.items : [];
  if (!items.length) return "";
  const domain = String(v.domain || "vertical");
  const sources = Array.isArray(v.sources) ? v.sources : [];
  const sourceLabel = sources.length
    ? sources.map(s => s.source || s.type || "").filter(Boolean).join(" · ")
    : "";
  const queryLabel = v.query ? ` · ${escapeHtml(String(v.query).slice(0, 60))}` : "";
  const rows = items.slice(0, 12).map(it => {
    const title = escapeHtml(String(it.title || "(untitled)"));
    const url = String(it.url || "");
    const titleHtml = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${title}</a>`
      : `<span>${title}</span>`;
    const meta = [];
    if (it.upvotes != null && it.upvotes !== "")
      meta.push(`<span class="vertical-meta-pill" title="Upvotes">▲ ${escapeHtml(String(it.upvotes))}</span>`);
    if (it.citations != null && it.citations !== "")
      meta.push(`<span class="vertical-meta-pill" title="Citations">⟲ ${escapeHtml(Number(it.citations).toLocaleString())}</span>`);
    if (it.year)
      meta.push(`<span class="vertical-meta-pill">${escapeHtml(String(it.year))}</span>`);
    if (it.arxiv_id)
      meta.push(`<span class="vertical-meta-pill">arXiv:${escapeHtml(String(it.arxiv_id))}</span>`);
    if (it.source && !sourceLabel.includes(it.source))
      meta.push(`<span class="vertical-meta-pill">${escapeHtml(String(it.source))}</span>`);
    const metaHtml = meta.length ? `<div class="vertical-row-meta">${meta.join("")}</div>` : "";
    const snippet = it.snippet ? `<div class="vertical-row-snippet">${escapeHtml(String(it.snippet))}</div>` : "";
    return `<div class="vertical-row">
       <div class="vertical-row-title">${titleHtml}</div>
       ${metaHtml}
       ${snippet}
     </div>`;
  }).join("");
  const moreLabel = items.length > 12
    ? `<div class="vertical-card-more">… +${items.length - 12} more</div>`
    : "";
  return `<div class="vertical-card vertical-domain-${escapeHtml(domain)}">
       <div class="vertical-card-header">
         <span class="vertical-card-icon">${_renderVerticalIcon(domain)}</span>
         <span class="vertical-card-title">${escapeHtml(domain.charAt(0).toUpperCase() + domain.slice(1))} sources</span>
         ${sourceLabel ? `<span class="vertical-card-sources">${escapeHtml(sourceLabel)}${queryLabel}</span>` : ""}
         <span class="vertical-card-count">${items.length}</span>
       </div>
       <div class="vertical-card-body">${rows}${moreLabel}</div>
     </div>`;
}

/* ★ Memory preview card — create_memory / update_memory / merge_memories.
 *   Always collapsible (even a partial name/tags-only update), with a
 *   dedicated themed card: a memory name, metadata chips (scope / id /
 *   source-count / tags), the description as a muted lead-in, and the
 *   Markdown-rendered body. Returns null when toolArgs can't be parsed so
 *   the caller falls through to the plain tool row. */
function _renderMemoryBlock(round, svg, q, compactionLabelHtml, rootPill, badgeHtml) {
  let pe = null;
  try { pe = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs; } catch (_) {}
  if (!pe || typeof pe !== 'object') return null;

  const name = typeof pe.name === 'string' ? pe.name.trim() : '';
  const desc = typeof pe.description === 'string' ? pe.description.trim() : '';
  const body = typeof pe.body === 'string' ? pe.body : '';
  const scope = typeof pe.scope === 'string' ? pe.scope.trim() : '';
  const tags = Array.isArray(pe.tags) ? pe.tags.filter((t) => typeof t === 'string' && t.trim()) : [];
  const memId = typeof pe.memory_id === 'string' ? pe.memory_id.trim() : '';
  const mergeIds = Array.isArray(pe.memory_ids) ? pe.memory_ids.filter(Boolean) : [];

  const chips = [];
  if (scope) chips.push(`<span class="ptool-memory-chip ptool-memory-chip-scope">${escapeHtml(scope)}</span>`);
  if (memId) chips.push(`<span class="ptool-memory-chip ptool-memory-chip-id" title="memory id">${escapeHtml(memId)}</span>`);
  if (mergeIds.length) chips.push(`<span class="ptool-memory-chip">${mergeIds.length} source${mergeIds.length !== 1 ? 's' : ''}</span>`);
  tags.forEach((t) => chips.push(`<span class="ptool-memory-chip ptool-memory-chip-tag">#${escapeHtml(t.trim())}</span>`));

  let inner = '';
  if (name) inner += `<div class="ptool-memory-name">${escapeHtml(name)}</div>`;
  if (chips.length) inner += `<div class="ptool-memory-chips">${chips.join('')}</div>`;
  if (desc) inner += `<div class="ptool-memory-desc">${escapeHtml(desc)}</div>`;
  if (body.trim()) inner += `<div class="ptool-memory-content md-content">${renderMarkdown(body)}</div>`;
  if (!inner) inner = `<div class="ptool-memory-empty">No additional preview for this update.</div>`;

  return `<details class="ptool-memory-block" data-rn="${round.roundNum}">
       <summary class="ptool-line ptool-memory-header">
         <span class="ptool-icon">${svg}</span>
         ${compactionLabelHtml}
         ${rootPill}
         <span class="ptool-text">${q}</span>
         ${badgeHtml}
       </summary>
       <div class="ptool-memory-body">${inner}</div>
     </details>`;
}

function _renderUnifiedToolLine(round, isSearching) {
  const svg = _getToolSvg(round);
  const td = _getToolDisplay(round);
  /* Preserve real newlines in the tool-call title — batch search/fetch
   * displays render one item per line so users can see every candidate
   * without elision. escapeHtml first (HTML-safe), THEN substitute
   * \n → <br> so the browser actually breaks the line. */
  const q = escapeHtml(round.query || "").replace(/\n/g, '<br>');
  const results = round.results || [];
  const meta = results[0] || {};
  const rootPill = _renderToolRootPill(round);
  /* ★ Harness self-repair badge — the backend auto-corrected this call's
   *   malformed arguments (truncated/invalid JSON, or schema-shape coercion).
   *   Surfacing it tells the user the displayed/executed args differ from
   *   the model's raw (broken) output. */
  const repairedBadge = _renderToolRepairedBadge(round);

  // ★ Async-swarm inbox injection — synthetic timeline row marking the point
  //   where the model received N <swarm-update> messages (sub-agent results)
  //   as a user message before its next turn. Not a real tool call, but
  //   rendered in chronological order so the user sees exactly when/what the
  //   model was handed. Collapsible: expands to the raw payloads.
  if (round._inboxInject) {
    return _renderInboxInjectRow(round);
  }

  // ★ Human Guidance — LLM is asking the user a question
  if (round.status === "awaiting_human" && round.guidanceId) {
    return _renderHumanGuidanceCard(round, svg);
  }

  // ★ Human Guidance — skipped (task ended before user answered)
  if (round.status === "done" && round.toolName === "ask_human" && round._hgSkipped) {
    const skippedQ = escapeHtml((round.guidanceQuestion || '').slice(0, 60));
    return `<div class="ptool-line hg-skipped-line">
      <span class="ptool-icon">${svg}</span>
      <span class="ptool-text">${td.label || 'Guidance'}${skippedQ ? ' — ' + skippedQ : ''}</span>
      <span class="ptool-badge ptool-badge-skip">未回答</span>
    </div>`;
  }

  // ★ Human Guidance — submitted but not yet confirmed by server (tool_result pending)
  if (round.status === "submitted" && round.toolName === "ask_human") {
    const respPreview = escapeHtml((round._hgUserResponse || '').slice(0, 80));
    return `<div class="ptool-line hg-submitted-line">
      <span class="ptool-icon">${svg}</span>
      <span class="ptool-text">${td.label || 'Guidance'}${respPreview ? ' — ' + respPreview : ''}</span>
      <span class="ptool-badge ptool-badge-done">✓ 已回答</span>
      <span class="hg-submitted-spinner" title="等待 AI 继续…"></span>
    </div>`;
  }

  // ★ Pending approval state — show approve/reject buttons
  if (round.status === "pending_approval" && round.approvalId) {
    const aid = escapeHtml(round.approvalId);
    const ameta = round.approvalMeta || {};
    let detailHtml = "";
    if (ameta.batchMode && ameta.editSummaries) {
      // ★ Batch apply_diff — show collapsible preview of all edits
      const edits = ameta.editSummaries;
      const maxPreviewLines = 12;
      let batchHtml = `<div class="ptool-batch-header">${edits.length} edit${edits.length > 1 ? "s" : ""} across ${ameta.path || "?"}</div>`;
      edits.forEach((ed, i) => {
        const sLines = (ed.search || "").split("\n");
        const rLines = (ed.replace || "").split("\n");
        const sShow = sLines.slice(0, maxPreviewLines);
        const rShow = rLines.slice(0, maxPreviewLines);
        let diffLines = "";
        sShow.forEach((l) => {
          diffLines += `<div class="ptool-diff-line ptool-diff-del"><span class="ptool-diff-sign">−</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
        });
        if (sLines.length > maxPreviewLines)
          diffLines += `<div class="ptool-diff-line ptool-diff-del ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${sLines.length - maxPreviewLines} more lines</span></div>`;
        diffLines += `<div class="ptool-diff-separator"></div>`;
        rShow.forEach((l) => {
          diffLines += `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
        });
        if (rLines.length > maxPreviewLines)
          diffLines += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${rLines.length - maxPreviewLines} more lines</span></div>`;
        const desc = ed.description ? escapeHtml(ed.description) : `Edit ${i + 1}`;
        const pathLabel = escapeHtml(ed.path || "?");
        batchHtml += `<details class="ptool-batch-edit"${i === 0 ? " open" : ""}>
          <summary class="ptool-batch-summary"><span class="ptool-batch-idx">#${i + 1}</span> <span class="ptool-batch-path">${pathLabel}</span> <span class="ptool-batch-desc">${desc}</span> <span class="ptool-batch-stats">${ed.searchLines || "?"}→${ed.replaceLines || "?"} lines</span></summary>
          <div class="ptool-diff-preview">${diffLines}</div>
        </details>`;
      });
      if ((ameta.editCount || edits.length) > edits.length)
        batchHtml += `<div class="ptool-batch-more">… and ${ameta.editCount - edits.length} more edits</div>`;
      detailHtml = `<div class="ptool-batch-preview">${batchHtml}</div>`;
    } else if (ameta.search != null && ameta.replace != null) {
      // Single apply_diff — show search→replace preview with line-by-line diff
      const searchLines = (ameta.search || "").split("\n");
      const replaceLines = (ameta.replace || "").split("\n");
      const totalSearchLines = ameta.searchLines || searchLines.length;
      const totalSearchChars = ameta.searchChars || ameta.search.length;
      const totalReplaceLines = ameta.replaceLines || replaceLines.length;
      const totalReplaceChars = ameta.replaceChars || ameta.replace.length;
      const maxLines = 30;
      const searchShow = searchLines.slice(0, maxLines);
      const replaceShow = replaceLines.slice(0, maxLines);
      let diffLines = "";
      searchShow.forEach((l) => {
        diffLines += `<div class="ptool-diff-line ptool-diff-del"><span class="ptool-diff-sign">−</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
      });
      if (totalSearchLines > maxLines)
        diffLines += `<div class="ptool-diff-line ptool-diff-del ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${totalSearchLines - maxLines} more lines (${totalSearchLines} lines · ${totalSearchChars.toLocaleString()} chars total)</span></div>`;
      diffLines += `<div class="ptool-diff-separator"></div>`;
      replaceShow.forEach((l) => {
        diffLines += `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
      });
      if (totalReplaceLines > maxLines)
        diffLines += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… ${totalReplaceLines - maxLines} more lines (${totalReplaceLines} lines · ${totalReplaceChars.toLocaleString()} chars total)</span></div>`;
      detailHtml = `<div class="ptool-diff-preview">${diffLines}</div>`;
    } else if (ameta.command != null) {
      // run_command — show command preview
      const cmdText = escapeHtml(ameta.command || "");
      detailHtml = `<div class="ptool-diff-preview"><pre class="ptool-cmd-code" style="margin:0;padding:8px 12px;font-size:12px;"><code>$ ${cmdText}</code></pre></div>`;
    } else if (ameta.contentPreview) {
      const previewLines = (ameta.contentPreview || "")
        .split("\n")
        .slice(0, 12);
      let previewContent = previewLines
        .map(
          (l) =>
            `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`,
        )
        .join("");
      if ((ameta.contentPreview || "").split("\n").length > 12)
        previewContent += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">… more lines</span></div>`;
      detailHtml = `<div class="ptool-diff-preview">${previewContent}<div class="ptool-write-meta">${ameta.contentLines || "?"} lines · ${(ameta.contentChars || 0).toLocaleString()} chars</div></div>`;
    }
    return `<div class="ptool-pending-wrap">
         <div class="ptool-line ptool-pending">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           <span class="ptool-badge ptool-badge-warn">awaiting approval</span>
         </div>
         ${detailHtml}
         <div class="ptool-approval-btns">
           <button class="ptool-approve-btn" onclick="event.stopPropagation();resolveWriteApproval('${aid}',true)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg> Approve</button>
           <button class="ptool-reject-btn" onclick="event.stopPropagation();resolveWriteApproval('${aid}',false)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Reject</button>
         </div>
       </div>`;
  }

  // ★ Timer Watcher: render collapsible poll checks
  if ((round._timerPolls && round._timerPolls.length > 0) || round._timerSkipCount) {
    return _renderTimerWatcherBlock(round, svg);
  }
  // Timer tool with "searching" status but no polls yet — show initial waiting
  // After reconnection, backend now includes _timerPolls in state snapshots,
  // so this state should be brief (only before the first poll fires).
  if (round.toolName === "timer_create" && round.status === "searching" && !round._timerPolls) {
    // ★ Try to recover timer polls from the API if timerId is known
    if (round._timerTimerId && !round._timerPollsRecoveryAttempted) {
      round._timerPollsRecoveryAttempted = true;
      _recoverTimerPolls(round);
    }
    return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">⏱️</span>
         <span class="ptool-text">${q || "Timer Watcher"}</span>
         <span class="ptool-badge ptool-badge-warn">waiting for first poll…</span>
         <span class="ptool-spinner"></span>
       </div>`;
  }

  // ★ Interactive stdin: subprocess is waiting for user keyboard input
  if (round.status === "awaiting_stdin" && round.stdinId) {
    const cmdText = escapeHtml(round.query || round.stdinCommand || "");
    const promptText = escapeHtml(round.stdinPrompt || "");
    const sid = escapeHtml(round.stdinId);
    return `<div class="ptool-cmd-block ptool-cmd-stdin" data-rn="${round.roundNum}">
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${svg}</span>
           ${rootPill}
           <span class="ptool-cmd-label">Waiting for input...</span>
           <span class="stdin-pulse"></span>
         </div>
         <pre class="ptool-cmd-code"><code>$ ${cmdText}</code></pre>
         ${promptText ? `<pre class="stdin-prompt-output"><code>${promptText}</code></pre>` : ''}
         <div class="stdin-input-area">
           <div class="stdin-input-row">
             <span class="stdin-caret">›</span>
             <input type="text" class="stdin-input" id="stdin-${sid}"
                    placeholder="Type your input here..."
                    onkeydown="if(event.key==='Enter'){event.preventDefault();submitStdinInput('${sid}',this.value)}" />
             <button class="stdin-submit-btn" onclick="submitStdinInput('${sid}', document.getElementById('stdin-${sid}').value)"
                     title="Send input">
               <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
             </button>
             <button class="stdin-eof-btn" onclick="submitStdinEof('${sid}')" title="Send EOF (close stdin)">
               EOF
             </button>
           </div>
         </div>
       </div>`;
  }

  if (isSearching) {
    // ★ run_command / code_exec: show running state with full command.
    //   If streaming output has started arriving via tool_progress events,
    //   render it live inside the block so the user can follow along.
    if (round.toolName === "run_command" || round.toolName === "code_exec") {
      const cmdText = escapeHtml(round.query || "");
      const partial = typeof round._partialOutput === "string" ? round._partialOutput : "";
      let liveOutHtml = "";
      if (partial) {
        // Cap the live-view length so the DOM stays snappy on very chatty
        // commands; the authoritative full output lands in meta.output on done.
        const MAX_LIVE = 20000;
        const shown = partial.length > MAX_LIVE
          ? ("… [" + (partial.length - MAX_LIVE).toLocaleString() + " earlier chars elided] …\n" + partial.slice(-MAX_LIVE))
          : partial;
        liveOutHtml = `<pre class="ptool-cmd-output ptool-cmd-output-live"><code>${escapeHtml(shown)}</code></pre>`;
      }
      return `<div class="ptool-cmd-block ptool-cmd-running">
           <div class="ptool-cmd-header">
             <span class="ptool-cmd-icon">${svg}</span>
             ${rootPill}
             <span class="ptool-cmd-label">Running...</span>
             <span class="ptool-spinner"></span>
           </div>
           <pre class="ptool-cmd-code"><code>$ ${cmdText}</code></pre>
           ${liveOutHtml}
         </div>`;
    }
    // ★ Web search: show orbit animation
    if (_isRoundSearch(round)) {
      return `<div class="ptool-line ptool-active ptool-search-line">
           <span class="ptool-icon"><div class="search-orbit-container" style="width:16px;height:16px"><div class="search-orbit-center" style="inset:4px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div></div></span>
           <span class="ptool-text">${q}</span>
           <span class="ptool-spinner"></span>
         </div>`;
    }
    return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">${svg}</span>
         ${rootPill}
         <span class="ptool-text">${q}</span>
         ${repairedBadge}
         <span class="ptool-spinner"></span>
       </div>`;
  }

  // ★ run_command / code_exec: render as inline terminal block with collapsible output
  if ((round.toolName === "run_command" || round.toolName === "code_exec") && (meta.command != null || meta.output != null)) {
    const cmd = escapeHtml(meta.command || round.query || "");
    const output = meta.output || "";
    const exitCode = meta.exitCode ?? "?";
    const timedOut = meta.timedOut || false;
    const isOk = exitCode === "0" || exitCode === 0;
    const statusCls = timedOut
      ? "ptool-cmd-timeout"
      : isOk
        ? "ptool-cmd-ok"
        : "ptool-cmd-err";
    const statusLabel = timedOut
      ? "timeout"
      : isOk
        ? "✓ done"
        : `✗ exit ${exitCode}`;
    let outputHtml = "";
    if (output) {
      outputHtml = `<div class="ptool-cmd-output-wrap">
           <div class="ptool-cmd-toggle" onclick="event.stopPropagation();var w=this.parentElement;w.classList.toggle('expanded');this.textContent=w.classList.contains('expanded')?'▾ Collapse':'▸ Show output';">▸ Show output</div>
           <pre class="ptool-cmd-output"><code>${escapeHtml(output)}</code></pre>
         </div>`;
    }
    return `<div class="ptool-cmd-block ${statusCls}" data-rn="${round.roundNum}">
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${svg}</span>
           ${rootPill}
           <span class="ptool-cmd-label">${round.toolName === "code_exec" ? "Code Execution" : "Command"}</span>
           <span class="ptool-cmd-status">${statusLabel}</span>
         </div>
         <pre class="ptool-cmd-code"><code>$ ${cmd}</code></pre>
         ${outputHtml}
       </div>`;
  }

  // ★ Web search / fetch with 0 results — show descriptive reason
  if ((_isRoundSearch(round) || _isRoundFetch(round)) && results.length === 0) {
    const diag = round.searchDiag;
    let badgeText, badgeCls, detailHtml = "";
    if (diag) {
      if (diag.reason === "network_error") {
        badgeText = "network error";
        badgeCls = "ptool-badge-err";
        detailHtml = `<div class="ptool-search-diag">All search engines failed — server may have limited internet access.</div>`;
      } else if (diag.reason === "partial_network_error") {
        const failedEngines = Object.keys(diag.engine_errors || {}).join(", ") || "some engines";
        badgeText = "partial failure";
        badgeCls = "ptool-badge-warn";
        detailHtml = `<div class="ptool-search-diag">Network errors from ${escapeHtml(failedEngines)}; other engines returned no matches.</div>`;
      } else if (diag.reason === "exception") {
        badgeText = "✗ error";
        badgeCls = "ptool-badge-err";
        detailHtml = `<div class="ptool-search-diag">Search encountered an internal error.</div>`;
      } else {
        badgeText = "no matches";
        badgeCls = "ptool-badge-warn";
        detailHtml = `<div class="ptool-search-diag">All engines responded but found no matching results. Try different keywords.</div>`;
      }
    } else {
      badgeText = "no results";
      badgeCls = "ptool-badge-warn";
    }
    return `<div class="ptool-line${detailHtml ? " ptool-line-with-diag" : ""}">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${q}</span>
         <span class="ptool-badge ${badgeCls}">${badgeText}</span>
         ${_tcPreviewBtn(round)}
         ${detailHtml}
       </div>`;
  }

  // ★ Web search / fetch with results — collapsible result list inside panel
  if ((_isRoundSearch(round) || _isRoundFetch(round)) && results.length > 0) {
    const _renderResultItem = (r) => {
      const fb = r.irrelevant
        ? `<span class="search-result-fetched" style="color:var(--text-muted);opacity:.6">✗ irrelevant</span>`
        : r.fetched
        ? `<span class="search-result-fetched${r.source === "PDF" ? " pdf" : ""}">✓ ${r.fetchedChars ? (r.fetchedChars > 1000 ? Math.round(r.fetchedChars / 1000) + "k" : r.fetchedChars) + " chars" : "fetched"}</span>`
        : "";
      return `<div class="search-result-item"><div class="search-result-title">${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a>` : `<span>${escapeHtml(r.title)}</span>`}<span class="search-result-source">${escapeHtml(r.source)}</span>${fb}</div>${r.snippet ? `<div class="search-result-snippet">${escapeHtml(r.snippet)}</div>` : ""}${r.url ? `<div class="search-result-url">${escapeHtml(r.url)}</div>` : ""}</div>`;
    };
    // ── Per-query grouping: when a batch search tagged each result with its
    //    source query (`_q`), render a subheader per query so the user can
    //    tell which web results came from which candidate term. Falls back
    //    to a flat list when only one query (or no `_q` tags) is present. ──
    let items;
    const _queryOrder = [];
    const _byQuery = new Map();
    for (const r of results) {
      const key = r._q || "";
      if (!_byQuery.has(key)) { _byQuery.set(key, []); _queryOrder.push(key); }
      _byQuery.get(key).push(r);
    }
    const _multiQuery = _queryOrder.filter(Boolean).length > 1;
    if (_multiQuery) {
      items = _queryOrder.map((key) => {
        const group = _byQuery.get(key);
        const groupItems = group.map(_renderResultItem).join("");
        const header = key
          ? `<div class="search-query-group-header"><span class="search-query-group-icon">🔍</span><span class="search-query-group-q">${escapeHtml(key)}</span><span class="search-query-group-count">${group.length}</span></div>`
          : "";
        return `<div class="search-query-group">${header}${groupItems}</div>`;
      }).join("");
    } else {
      items = results.map(_renderResultItem).join("");
    }
    // ── Vertical card: HF Papers / Semantic Scholar / arXiv / etc. ──
    let verticalHtml = "";
    const verts = [];
    // A single vertical is one {domain, sources, items} dict; batch
    // web_search carries several. The streaming prefetch path may wrap
    // them as {batch:[...]}, so unwrap that here — otherwise the card
    // renders empty (no items) and the badge falls back to bare "auto".
    const _pushVert = (v) => {
      if (!v || typeof v !== "object") return;
      if (Array.isArray(v.batch)) { v.batch.forEach(_pushVert); return; }
      verts.push(v);
    };
    if (round.vertical) _pushVert(round.vertical);
    if (Array.isArray(round.verticals)) round.verticals.forEach(_pushVert);
    // Batch web_search emits one vertical record PER query — for a 5-query
    // academic batch that's 5 near-identical "Academic" cards with heavily
    // overlapping items. Merge cards that share a domain into one, dedup
    // items by url/arxiv_id/title, and keep the highest upvote/citation
    // count seen for each. Preserves first-seen order.
    const _mergedVerts = (() => {
      const byDomain = new Map();
      for (const v of verts) {
        const dom = String(v.domain || "vertical");
        if (!byDomain.has(dom)) {
          byDomain.set(dom, { domain: dom, sources: [], items: [], _seen: new Map() });
        }
        const acc = byDomain.get(dom);
        for (const s of (Array.isArray(v.sources) ? v.sources : [])) {
          const key = (s.source || s.type || "") + "|" + (s.identifier || "");
          if (!acc.sources.some(x => ((x.source || x.type || "") + "|" + (x.identifier || "")) === key))
            acc.sources.push(s);
        }
        for (const it of (Array.isArray(v.items) ? v.items : [])) {
          const k = it.url || it.arxiv_id || it.title || JSON.stringify(it);
          const prev = acc._seen.get(k);
          if (!prev) { acc._seen.set(k, it); acc.items.push(it); }
          else {
            const num = (x) => (x == null || x === "" ? -1 : Number(x) || -1);
            if (num(it.upvotes) > num(prev.upvotes)) prev.upvotes = it.upvotes;
            if (num(it.citations) > num(prev.citations)) prev.citations = it.citations;
          }
        }
      }
      return [...byDomain.values()];
    })();
    // Sort each merged card's items by upvotes desc, then citations desc.
    for (const v of _mergedVerts) {
      const num = (x) => (x == null || x === "" ? -1 : Number(x) || -1);
      v.items.sort((a, b) => (num(b.upvotes) - num(a.upvotes)) || (num(b.citations) - num(a.citations)));
    }
    for (const v of _mergedVerts) {
      verticalHtml += _renderVerticalCard(v);
    }

    // ── Engine breakdown: show raw per-engine URLs (before dedup/filter) ──
    let engineBkdnHtml = "";
    const eb = round.engineBreakdown;
    if (eb && typeof eb === "object") {
      const engines = Object.keys(eb);
      if (engines.length > 0) {
        const totalRaw = engines.reduce((s, e) => s + (eb[e] ? eb[e].length : 0), 0);
        const ebInner = engines.map((eng) => {
          const urls = eb[eng] || [];
          const urlItems = urls.map((u) =>
            `<div class="eb-url-item"><a href="${escapeHtml(u.url)}" target="_blank" rel="noopener">${escapeHtml(u.title || u.url)}</a><div class="eb-url-text">${escapeHtml(u.url)}</div></div>`
          ).join("");
          return `<div class="eb-engine"><div class="eb-engine-name">${escapeHtml(eng)} <span class="eb-engine-count">(${urls.length})</span></div><div class="eb-engine-urls">${urlItems}</div></div>`;
        }).join("");
        engineBkdnHtml = `<div class="eb-section">
          <div class="eb-toggle" onclick="event.stopPropagation();this.parentElement.classList.toggle('eb-expanded')">🔍 Engine Sources <span class="eb-total">${totalRaw} raw → ${results.length} final</span> <span class="eb-arrow">▸</span></div>
          <div class="eb-content">${ebInner}</div>
        </div>`;
      }
    }
    return `<div class="ptool-results-block" data-rn="${round.roundNum}">
         <div class="ptool-line ptool-results-header" onclick="if(event.target.closest('[data-tc-preview]'))return;event.stopPropagation();this.parentElement.classList.toggle('expanded')">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           ${verts.length ? (()=>{const doms=[...new Set(verts.map(v=>v.domain||'').filter(Boolean))];return `<span class="ptool-badge vertical-badge" title="Vertical domain data">vertical: ${escapeHtml(doms.join(' · ') || 'auto')}</span>`;})() : ''}
           <span class="ptool-badge ptool-badge-info">${results.length} result${results.length !== 1 ? "s" : ""}</span>
           ${_tcPreviewBtn(round)}
           <span class="ptool-results-toggle">▼</span>
         </div>
         <div class="ptool-results-content">${verticalHtml}${items}${engineBkdnHtml}</div>
       </div>`;
  }

  // ★ read_files image(s): render inline thumbnails when the backend
  //   attached data URIs (meta.imageDataUris). Each descriptor carries a
  //   full data: URL the browser can render directly.
  if (round.toolName === "read_files" && Array.isArray(meta.imageDataUris) && meta.imageDataUris.length) {
    const imgs = meta.imageDataUris.filter((d) => d && d.uri);
    if (imgs.length) {
      const cards = imgs.map((d) => {
        const cap = escapeHtml(d.filename || d.format || "");
        return `<div class="imagegen-card">
             <img src="${escapeHtml(d.uri)}" alt="${cap}" loading="lazy"
                  onclick="_openImageFullscreen(this.src)" />
             ${cap ? `<div class="imagegen-card-footer"><span class="ig-prompt">${cap}</span></div>` : ""}
           </div>`;
      }).join("");
      return `<div class="ptool-imagegen-block" data-rn="${round.roundNum}">
           <div class="ptool-line ptool-imagegen-header">
             <span class="ptool-icon">${svg}</span>
             <span class="ptool-text">${q}</span>
             ${meta.badge ? `<span class="ptool-badge ptool-badge-info">${escapeHtml(meta.badge)}</span>` : ""}
             ${_tcPreviewBtn(round)}
           </div>
           ${cards}
         </div>`;
    }
  }

  // ★ Image generation / editing: render inline image card.
  //   The two functions are visually distinguished by a mode theme:
  //   • generate → magenta "Generated" theme + framed-photo icon
  //   • edit     → cyan "Edited" theme + wand icon + before→after strip
  if (_isRoundImageGen(round)) {
    const isEdit = meta.imageMode === "edit";
    const modeCls = isEdit ? "ig-mode-edit" : "ig-mode-generate";
    const modeChip = isEdit
      ? `<span class="ig-mode-chip ig-mode-chip-edit" title="Edited an existing image">${_imageEditChipSvg}Edited</span>`
      : `<span class="ig-mode-chip ig-mode-chip-gen" title="Generated from a text prompt">${_imageGenChipSvg}Generated</span>`;
    const srcUrl = meta.imageSourceUrl || "";
    const imgUri = meta.imageDataUri || "";
    const imgErr = meta.imageError || "";
    const prompt = meta.imagePrompt || escapeHtml(round.query || "").replace(/^🎨\s*Generating[^:]*:\s*/i, "");
    const imgAR = meta.imageAspectRatio || "";
    const imgRes = meta.imageResolution || "";
    const paramsBadges = (imgAR || imgRes)
      ? `<span class="ptool-badge ptool-badge-info ig-params">${imgAR ? escapeHtml(imgAR) : ""}${imgAR && imgRes ? " · " : ""}${imgRes ? escapeHtml(imgRes) : ""}</span>`
      : "";
    if (imgUri) {
      const projPath = meta.imageProjectPath || "";
      const svgUrl = meta.svgSavedUrl || "";
      const svgPath = meta.svgProjectPath || "";
      const hasSvg = !!(svgUrl || svgPath);
      const svgBadge = hasSvg
        ? `<span class="ptool-badge ptool-badge-info ig-svg-badge" title="${escapeHtml("SVG version generated" + (svgPath ? ": " + svgPath : ""))}">SVG</span>`
        : "";
      const svgBtn = svgUrl
        ? `<button class="ig-action-btn" onclick="event.stopPropagation();window.open('${escapeHtml(svgUrl)}','_blank')" title="${escapeHtml("Open SVG" + (svgPath ? " — " + svgPath : ""))}">SVG</button>`
        : "";
      const pathBadges = [
        projPath ? `<span class="ig-path-chip" title="Saved to project: ${escapeHtml(projPath)}"><span class="ig-path-icon">🖼</span>${escapeHtml(projPath)}</span>` : "",
        svgPath ? `<span class="ig-path-chip ig-path-chip-svg" title="SVG saved to project: ${escapeHtml(svgPath)}"><span class="ig-path-icon">⬡</span>${escapeHtml(svgPath)}</span>` : "",
      ].filter(Boolean).join("");
      const pathFooter = pathBadges
        ? `<div class="ig-path-bar">${pathBadges}</div>`
        : "";
      // For edits with a loadable source, show a before→after strip so the
      // transformation is obvious at a glance; otherwise a single result image.
      const imageArea = (isEdit && srcUrl)
        ? `<div class="ig-beforeafter">
               <figure class="ig-ba-item">
                 <img src="${escapeHtml(srcUrl)}" alt="source image" loading="lazy"
                      onclick="event.stopPropagation();_openImageFullscreen(this.src)" />
                 <figcaption>Before</figcaption>
               </figure>
               <span class="ig-ba-arrow" aria-hidden="true">→</span>
               <figure class="ig-ba-item">
                 <img src="${imgUri}" alt="${escapeHtml((prompt || "").slice(0, 100))}" loading="lazy"
                      onclick="event.stopPropagation();_openImageFullscreen(this.src)" />
                 <figcaption>After</figcaption>
               </figure>
             </div>`
        : `<img src="${imgUri}" alt="${escapeHtml((prompt || "").slice(0, 100))}" loading="lazy"
                  onclick="_openImageFullscreen(this.src)" />`;
      return `<div class="ptool-imagegen-block ${modeCls}" data-rn="${round.roundNum}">
           <div class="ptool-line ptool-imagegen-header">
             <span class="ptool-icon">${svg}</span>
             <span class="ptool-text">${q}</span>
             ${modeChip}
             ${paramsBadges}
             ${svgBadge}
             <span class="ptool-badge ptool-badge-ok">${escapeHtml(meta.badge || "✓ done")}</span>
             ${_tcPreviewBtn(round)}
           </div>
           <div class="imagegen-card">
             ${imageArea}
             <div class="imagegen-card-footer">
               <span class="ig-prompt" title="${escapeHtml(prompt)}">${escapeHtml(prompt || "")}</span>
               <div class="ig-actions">
                 ${svgBtn}
                 <button class="ig-action-btn" onclick="event.stopPropagation();_downloadGenImage(this)" title="Download PNG">⬇</button>
                 <button class="ig-action-btn" onclick="event.stopPropagation();_openImageFullscreen(this.closest('.imagegen-card').querySelector('.ig-beforeafter .ig-ba-item:last-child img, img').src)" title="Fullscreen">⛶</button>
               </div>
             </div>
             ${pathFooter}
           </div>
         </div>`;
    } else if (imgErr) {
      return `<div class="ptool-imagegen-block ptool-imagegen-error ${modeCls}" data-rn="${round.roundNum}">
           <div class="ptool-line">
             <span class="ptool-icon">${svg}</span>
             <span class="ptool-text">${q}</span>
             ${modeChip}
             <span class="ptool-badge ptool-badge-err">failed</span>
             ${_tcPreviewBtn(round)}
           </div>
           <div class="imagegen-error">
             <div class="ig-error-title">${isEdit ? "Image editing failed" : "Image generation failed"}</div>
             <div class="ig-error-text">${escapeHtml(imgErr)}</div>
           </div>
         </div>`;
    }
    // In-progress: no image yet, no error — show animated working state
    const progressBadge = meta.badge || (isEdit ? "editing…" : "generating…");
    const progressCls = progressBadge.includes("rate limited") ? "ptool-badge-err" : "ptool-badge-warn";
    return `<div class="ptool-imagegen-block ptool-imagegen-loading ${modeCls}" data-rn="${round.roundNum}">
         <div class="ptool-line ptool-active">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           ${modeChip}
           ${paramsBadges}
           <span class="ptool-badge ${progressCls}">${escapeHtml(progressBadge)}</span>
           <span class="ptool-spinner"></span>
         </div>
       </div>`;
  }

  // Determine badge
  let badgeHtml = "";
  if (meta.badge) {
    const isWrite =
      round.toolName === "write_file" || round.toolName === "apply_diff" ||
      round.toolName === "apply_diffs" || round.toolName === "insert_content" ||
      round.toolName === "insert_contents";
    const ok = meta.writeOk !== false;
    /* ★ A successful memory op (meta.memoryOk, set by the backend memory
     * handler) reads as a "save" — show the solid green OK badge, same as
     * a write tool, instead of the neutral yellow info badge. */
    const isMemoryOk = meta.memoryOk === true;
    /* ★ await_agents timeout: amber warning badge so a partial result
     * (wait cut short by the hard cap) never looks like a clean "done".
     * Backend sets meta.awaitTimedOut in the await_agents post_build hook. */
    const cls = meta.awaitTimedOut
      ? "ptool-badge-warn"
      : isWrite
      ? ok
        ? "ptool-badge-ok"
        : "ptool-badge-err"
      : isMemoryOk
      ? "ptool-badge-ok"
      : "ptool-badge-info";
    badgeHtml = `<span class="ptool-badge ${cls}">${escapeHtml(meta.badge)}</span>`;
  } else if (round.toolTokens) {
    /* ★ Per-tool token count — emitted by lib/tasks_pkg/tool_dispatch.py
     * tool_complete event. Falls back to fetchedChars on older rounds. */
    const t = round.toolTokens;
    const txt = t >= 1000 ? (t / 1000).toFixed(t >= 10000 ? 0 : 1) + "k tok" : t + " tok";
    const fcTitle = meta.fetchedChars ? ` (${meta.fetchedChars.toLocaleString()} chars)` : "";
    badgeHtml = `<span class="ptool-badge ptool-badge-info" title="Tokens consumed by this tool result${fcTitle}">${txt}</span>`;
  } else if (meta.fetchedChars) {
    const fc = meta.fetchedChars;
    const txt = fc > 1000 ? Math.round(fc / 1000) + "k chars" : fc + " chars";
    badgeHtml = `<span class="ptool-badge ptool-badge-info">${txt}</span>`;
  }
  /* ★ Compaction badge — flag tool calls whose content has been replaced
   * with a placeholder (L0 = budget/persist to disk, L1 = micro_compact
   * cold-tail). The model now sees only a short marker; clicking the
   * preview button still opens the original toolContent. */
  /* The old per-row "🗜 L1 280k→2k" badge previously appended to
   * badgeHtml was REMOVED — the inline COMPACTED L1 pill rendered
   * before the tool name (see compactionLabelHtml below) carries the
   * same information at higher visibility, and showing both clutters
   * the row. */
  // ★ Generic tool done with no results and no badge — show ✓ done
  if (!badgeHtml && !_isRoundProject(round) && !_isRoundBrowser(round) && results.length === 0) {
    const elapsed = round._elapsed ? ` · ${round._elapsed}` : "";
    badgeHtml = `<span class="ptool-badge ptool-badge-ok">✓ done${elapsed}</span>`;
  }

  /* COMPACTION LABEL (mandatory per UX directive):
   *
   * If this row's tool result has been replaced by a server-side
   * compaction placeholder (round.compactionLayer is set by the
   * `tool_compacted` SSE event), render an UNAMBIGUOUS solid-color
   * pill that says exactly what happened. NO opacity fading, NO
   * subtle accent strips that read as decoration — the user has to
   * know at a glance: "this tool result is no longer in the model's
   * view; here is the layer responsible."
   *
   * Layers (from compaction.py):
   *   L0 — born too big, persisted to disk, model never saw full text
   *   L1 — was visible once, aged out of the hot tail (60 rounds back)
   *   L3 — replaced by an LLM-generated summary (transcript_archive)
   *
   * The label is placed BEFORE the tool name so it's the first thing
   * the eye lands on, and uses solid colors at full opacity per the
   * "no transparency" rule. */
  let compactionLabelHtml = "";
  if (round.compactionLayer) {
    const layer = round.compactionLayer;       // 'L0' | 'L1' | 'L3'
    const layerLc = layer.toLowerCase();
    const fromTok = round.compactedFromChars
      ? Math.max(1, Math.round(round.compactedFromChars / 4))
      : null;
    const toTok = round.compactedToChars
      ? Math.max(1, Math.round(round.compactedToChars / 4))
      : null;
    const reduction = (fromTok && toTok)
      ? ` ${_formatTok(fromTok)}→${_formatTok(toTok)}`
      : "";
    const layerExplain = {
      L0: 'Result too large at fetch time — replaced with a placeholder before the model ever saw the full text.',
      L1: 'Aged out of the hot tail (60 most-recent tool calls) — replaced with a short marker on the next LLM call.',
      L3: 'Replaced by an LLM-generated summary in the transcript archive.',
    }[layer] || 'This tool result has been replaced by a placeholder.';
    const tip = `Compacted (${layer})${reduction ? ' — ' + reduction.trim() + ' tokens' : ''}\n${layerExplain}`;
    compactionLabelHtml =
      `<span class="ptool-compaction-label ptool-compaction-${layerLc}" title="${tip.replace(/"/g, '&quot;')}">` +
        `<span class="ptool-compaction-text">COMPACTED ${layer}</span>` +
        (reduction ? `<span class="ptool-compaction-delta">${reduction.trim()}</span>` : '') +
      `</span>`;
  }
  // ★ create_memory / update_memory / merge_memories — collapsible,
  //   Markdown-rendered preview of the saved memory body itself (mirrors the
  //   apply_diff expand block). The opaque description-snippet "Preview" was
  //   useless to users; expanding now shows the actual memory text, well-
  //   rendered. The full body lives in round.toolArgs.body. update_memory may
  //   omit body on a partial (name/tags-only) update — the body.trim() guard
  //   below falls through to the normal row in that case.
  if ((round.toolName === "create_memory" || round.toolName === "update_memory" || round.toolName === "merge_memories") && round.toolArgs) {
    const memHtml = _renderMemoryBlock(round, svg, q, compactionLabelHtml, rootPill, badgeHtml);
    if (memHtml) return memHtml;
  }

  // ★ write_file — collapsible inline preview of the written content,
  //   mirroring the apply_diff expand-on-click block. The full content
  //   lives in round.toolArgs.content; render it as added lines so the
  //   user can review what was written instead of an opaque "Preview".
  if (round.toolName === "write_file" && round.toolArgs) {
    let pe = null;
    try { pe = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs; } catch (_) {}
    if (pe && typeof pe.content === 'string' && pe.content.length) {
      const diffHtml = _renderLineDiff("", pe.content);
      if (diffHtml) {
        return `<details class="ptool-batch-done-block" data-rn="${round.roundNum}">
             <summary class="ptool-line ptool-batch-done-header">
               <span class="ptool-icon">${svg}</span>
               ${compactionLabelHtml}
               ${rootPill}
               <span class="ptool-text">${q}</span>
               ${badgeHtml}
             </summary>
             <div class="ptool-batch-done-list">
               <div class="ptool-batch-done-single">${diffHtml}</div>
             </div>
           </details>`;
      }
    }
  }

  // ★ Single apply_diff / insert_content — collapsible inline diff
  if (!meta.editSummaries && (round.toolName === "apply_diff" || round.toolName === "insert_content") && round.toolArgs) {
    let pe = null;
    try { pe = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs; } catch (_) {}
    if (pe && (pe.search || pe.anchor)) {
      const isInsert = !pe.search && pe.anchor;
      const oldText = pe.search || pe.anchor || "";
      const newText = isInsert
        ? ((pe.position === "before" ? (pe.content + "\n") : "") + (pe.anchor || "") + (pe.position !== "before" ? ("\n" + pe.content) : ""))
        : (pe.replace || "");
      const diffHtml = _renderLineDiff(oldText, newText);
      if (diffHtml) {
        return `<details class="ptool-batch-done-block" data-rn="${round.roundNum}">
             <summary class="ptool-line ptool-batch-done-header">
               <span class="ptool-icon">${svg}</span>
               ${compactionLabelHtml}
               ${rootPill}
               <span class="ptool-text">${q}</span>
               ${badgeHtml}
             </summary>
             <div class="ptool-batch-done-list">
               <div class="ptool-batch-done-single">${diffHtml}</div>
             </div>
           </details>`;
      }
    }
  }

  // ★ Batch edit tools (apply_diffs / insert_contents) — collapsible per-edit list
  //   Guard on toolName: editSummaries is only meaningful for the batch edit
  //   tools. Without this guard, ANY round whose results[0] happens to carry
  //   an editSummaries array (e.g. a tool_result leaked from a sub-agent's
  //   apply_diffs grafted onto a same-roundNum run_command — see
  //   sse_handlers_tool.js roundNum fallback) renders as a batch-edit block.
  const _isBatchEditTool = round.toolName === "apply_diffs" || round.toolName === "insert_contents";
  if (_isBatchEditTool && meta.editSummaries && Array.isArray(meta.editSummaries) && meta.editSummaries.length > 1) {
    const edits = meta.editSummaries;
    let parsedEdits = null;
    if (round.toolArgs) {
      try {
        const args = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs;
        if (args.edits && Array.isArray(args.edits)) parsedEdits = args.edits;
      } catch (_) {}
    }
    // The header already names the target file (e.g. "Patch /a/b/c.sh (2 edits)").
    // Only repeat a per-row path when edits span DIFFERENT files, and then show
    // just the basename — never the full absolute path, which would starve the
    // description column down to one character per line.
    const _multiFile = edits.some(e => (e.path || "") !== (edits[0].path || ""));
    let itemsHtml = "";
    edits.forEach((ed, i) => {
      const statusIcon = ed.status === "fail"
        ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
        : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      const statusCls = ed.status === "fail" ? "ptool-batch-fail" : "ptool-batch-ok";
      const rawDesc = ed.description ? _stripPathPrefixFromDesc(ed.description, ed.path) : "";
      const desc = rawDesc ? escapeHtml(rawDesc) : `Edit ${i + 1}`;
      const fullPath = ed.path || "";
      const baseName = fullPath ? (fullPath.split("/").filter(Boolean).pop() || fullPath) : "";
      const pathHtml = (_multiFile && baseName)
        ? `<span class="ptool-batch-path" title="${escapeHtml(fullPath)}">${escapeHtml(baseName)}</span>`
        : "";
      let diffHtml = "";
      if (ed.status !== "fail" && parsedEdits && parsedEdits[i]) {
        const pe = parsedEdits[i];
        const isInsert = !pe.search && pe.anchor;
        const oldText = pe.search || pe.anchor || "";
        const newText = isInsert
          ? ((pe.position === "before" ? (pe.content + "\n") : "") + (pe.anchor || "") + (pe.position !== "before" ? ("\n" + pe.content) : ""))
          : (pe.replace || "");
        if (oldText || newText) diffHtml = _renderLineDiff(oldText, newText);
      }
      itemsHtml += `<details class="ptool-batch-done-edit ${statusCls}">
        <summary class="ptool-batch-done-summary">
          <span class="ptool-batch-status">${statusIcon}</span>
          <span class="ptool-batch-idx">${i + 1}</span>
          <span class="ptool-batch-desc">${desc}</span>
          ${pathHtml}
        </summary>
        ${diffHtml}
      </details>`;
    });
    return `<details class="ptool-batch-done-block" open data-rn="${round.roundNum}">
         <summary class="ptool-line ptool-batch-done-header">
           <span class="ptool-icon">${svg}</span>
           ${compactionLabelHtml}
           ${rootPill}
           <span class="ptool-text">${q}</span>
           ${badgeHtml}
         </summary>
         <div class="ptool-batch-done-list">${itemsHtml}</div>
       </details>`;
  }

  return `<div class="ptool-line">
       <span class="ptool-icon">${svg}</span>
       ${compactionLabelHtml}
       ${rootPill}
       <span class="ptool-text">${q}</span>
       ${repairedBadge}
       ${badgeHtml}
       ${_tcPreviewBtn(round)}
     </div>`;
}

/* Format a token count compactly: 12000 → "12k", 1500000 → "1.5M". */
function _formatTok(n) {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
  return String(n | 0);
}

// ★ Backwards compat alias
const _renderProjectToolLine = _renderUnifiedToolLine;

/* ── Timer poll recovery: fetch poll log from API when _timerPolls is missing ──
   This handles edge cases where the state snapshot doesn't include polls
   (e.g. old server version, or server restarted and lost in-memory state). */
async function _recoverTimerPolls(round) {
  const timerId = round._timerTimerId;
  if (!timerId) return;
  try {
    const data = await Api.timer.status(timerId, 50);
    if (!data) return;
    const polls = data.poll_log || [];
    if (polls.length > 0) {
      // poll_log is newest-first from the API, reverse for chronological order
      const chronological = [...polls].reverse();
      const recoveredPolls = chronological.map((p, idx) => ({
        pollNum: p.poll_num || p.pollNum || (idx + 1),
        decision: p.decision || 'wait',
        reason: (p.reason || '').slice(0, 200),
        tokensUsed: p.tokens_used || 0,
        timerId: timerId,
        ts: p.poll_time ? new Date(p.poll_time).getTime() : Date.now(),
      }));
      const triggered = chronological.some(p => p.decision === 'ready');

      // Apply to the round object — but also search the active conv's
      // toolRounds in case the round reference was replaced by a state event
      round._timerPolls = recoveredPolls;
      if (triggered) { round._timerTriggered = true; round.status = 'done'; }

      if (activeConvId) {
        const conv = conversations.find(c => c.id === activeConvId);
        const lastMsg = conv?.messages?.[conv.messages.length - 1];
        if (lastMsg?.toolRounds) {
          const liveRound = lastMsg.toolRounds.find(r =>
            r.toolName === 'timer_create' && r._timerTimerId === timerId && !r._timerPolls
          );
          if (liveRound && liveRound !== round) {
            liveRound._timerPolls = recoveredPolls;
            if (triggered) { liveRound._timerTriggered = true; liveRound.status = 'done'; }
          }
        }
        twUpdate(activeConvId);
      }
      console.info(`[Timer] Recovered ${recoveredPolls.length} polls for timer ${timerId.slice(0,12)}`);
    }
  } catch (e) {
    console.debug('[Timer] Poll recovery failed:', e.message);
  }
}

/* ── Timer Watcher Block ──
   Renders the timer_create tool call as a collapsible panel showing
   each poll check (wait/ready/error) with timestamps and reasons.
   While polling, shows a live "watching…" header; after trigger, shows "✓ triggered". */
function _renderTimerWatcherBlock(round, svg) {
  const polls = round._timerPolls || [];
  const isActive = round.status === "searching";
  const triggered = round._timerTriggered;
  const timerId = round._timerTimerId || "";
  const totalPolls = polls.filter(p => p.decision !== "started").length;
  const timerIdShort = timerId ? timerId.slice(0, 12) : "";

  // Header
  let headerLabel, headerCls;
  if (triggered) {
    headerLabel = `⏱️ Timer ${timerIdShort} — ✅ triggered after ${totalPolls} poll${totalPolls !== 1 ? "s" : ""}`;
    headerCls = "timer-watcher-triggered";
  } else if (round._timerOrphaned) {
    headerLabel = `⏱️ Timer ${timerIdShort} — ⚠️ task interrupted (${totalPolls} poll${totalPolls !== 1 ? "s" : ""}, timer still active in background)`;
    headerCls = "timer-watcher-orphaned";
  } else if (isActive) {
    const skipN = round._timerSkipCount || 0;
    const skipSuffix = skipN > 0 ? `, ${skipN} skipped` : "";
    headerLabel = `⏱️ Timer ${timerIdShort} — watching… (${totalPolls} poll${totalPolls !== 1 ? "s" : ""}${skipSuffix})`;
    headerCls = "timer-watcher-active";
  } else {
    headerLabel = `⏱️ Timer ${timerIdShort} — ${round.status || "done"} (${totalPolls} polls)`;
    headerCls = "";
  }

  // Build poll lines (most recent first for readability)
  const reversed = [...polls].reverse();
  const MAX_VISIBLE = 5;
  const visible = reversed.slice(0, MAX_VISIBLE);
  const hidden = reversed.length - MAX_VISIBLE;

  let pollLines = "";
  for (const p of visible) {
    let icon, cls;
    if (p.decision === "started") {
      icon = "🔔"; cls = "timer-poll-started";
    } else if (p.decision === "ready") {
      icon = "✅"; cls = "timer-poll-ready";
    } else if (p.decision === "error") {
      icon = "❌"; cls = "timer-poll-error";
    } else {
      icon = "⏳"; cls = "timer-poll-wait";
    }
    const ts = p.ts ? new Date(p.ts).toLocaleTimeString() : "";
    const reason = escapeHtml((p.reason || "").slice(0, 120));
    const pollLabel = p.decision === "started" ? "" : `#${p.pollNum}`;
    const tokens = p.tokensUsed ? ` · ${p.tokensUsed} tok` : "";
    pollLines += `<div class="timer-poll-line ${cls}">
      <span class="timer-poll-icon">${icon}</span>
      <span class="timer-poll-num">${pollLabel}</span>
      <span class="timer-poll-reason">${reason}</span>
      <span class="timer-poll-meta">${ts}${tokens}</span>
    </div>`;
  }

  let hiddenHtml = "";
  if (hidden > 0) {
    hiddenHtml = `<div class="timer-poll-hidden">${hidden} earlier check${hidden !== 1 ? "s" : ""} hidden</div>`;
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
      <span class="timer-poll-icon">💤</span>
      <span class="timer-poll-num">${lastPollNum ? `#${lastPollNum}` : ""}</span>
      <span class="timer-poll-reason">${round._timerSkipCount} poll${round._timerSkipCount !== 1 ? "s" : ""} skipped — check_command output unchanged</span>
      <span class="timer-poll-meta">${skipTs}</span>
    </div>`;
  }

  const uid = "tmr-r" + round.roundNum;
  const expandedByDefault = isActive;  // auto-expand while active
  return `<div class="timer-watcher-block ${headerCls}" data-rn="${round.roundNum}">
       <div class="timer-watcher-header" onclick="event.stopPropagation();var w=document.getElementById('${uid}-wrap');w.classList.toggle('expanded');var t=this.querySelector('.timer-toggle');if(t)t.textContent=w.classList.contains('expanded')?'▾':'▸';">
         <span class="timer-watcher-label">${headerLabel}</span>
         ${isActive ? '<span class="ptool-spinner"></span>' : ''}
         <span class="timer-toggle">${expandedByDefault ? '▾' : '▸'}</span>
       </div>
       <div class="timer-watcher-body${expandedByDefault ? ' expanded' : ''}" id="${uid}-wrap">
         ${pollLines}${hiddenHtml}${skipTrailer}
       </div>
     </div>`;
}

function _renderUnifiedGroup(allRounds) {
  const anyActive = allRounds.some((r) => r.status === "searching" || r._swarmActive);
  const count = allRounds.length;
  const headerLabel = anyActive
    ? `Working… (${count})`
    : `${count} tool${count > 1 ? "s" : ""} used`;
  /* Render one slot per round, swarm rounds use the full agent
   * dashboard (`_buildSwarmPanelHTML`), everything else uses the
   * compact tool-line.  Each slot wears `data-prn` so the streaming
   * sync path (see `_syncToolRoundsDOM`) can locate and update it
   * by round number. */
  const _renderSlot = (r) => {
    const inner = _isRoundSwarm(r)
      ? _buildSwarmPanelHTML(r)
      : _renderUnifiedToolLine(r, r.status === "searching");
    const swarmAttr = _isRoundSwarm(r) ? ' data-prn-kind="swarm"' : '';
    return `<div data-prn="${r.roundNum}"${swarmAttr}>${inner}</div>`;
  };
  const STATIC_LIMIT = 100;
  let lines, truncHtml = "";
  if (!anyActive && count > STATIC_LIMIT) {
    const tail = allRounds.slice(-50);
    lines = tail.map(_renderSlot).join("");
    const hiddenN = count - 50;
    truncHtml = `<div class="ptool-truncated" data-hidden-count="${hiddenN}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${hiddenN} earlier tool calls hidden — click to expand</span></div>`;
  } else {
    lines = allRounds.map(_renderSlot).join("");
  }
  return `<div class="ptool-panel${anyActive ? " ptool-panel-active" : ""}">
       <div class="ptool-panel-header">
         <span class="ptool-panel-label">${headerLabel}</span>
       </div>
       <div class="ptool-panel-body" data-full-count="${count}">${truncHtml}${lines}</div>
     </div>`;
}

// ★ Backwards compat aliases
const _renderProjectGroup = _renderUnifiedGroup;
const _renderBrowserGroup = _renderUnifiedGroup;

// ── Tool content preview button ──
function _tcPreviewBtn(round) {
  if (!round || !round.toolContent) return "";
  return `<button class="tc-preview-btn" data-tc-preview data-tc-rn="${round.roundNum}" data-tc-tcid="${escapeHtml(round.toolCallId || '')}" title="Preview tool content">Preview</button>`;
}

// ── Memory Prefetch indicator (chip above tool panel) ──
// Rendered in the streaming bubble AND in the finished assistant message.
// Shows that a cheap model is (or was) filtering memories in the background.
/**
 * Render the MCP login-hint chip.
 *   phase = awaiting_approval | approved | denied | timeout | done
 * The banner appears as soon as the SSE tool_start for an MCP login-style
 * tool arrives, so the user knows to check their phone for the push
 * notification BEFORE the subprocess times out (up to 5 min wait).
 */
function renderMcpLoginHintHtml(lh) {
  if (!lh) return '';
  const phase = lh.phase || 'awaiting_approval';
  const user = lh.username || '';
  let icon = '📱';
  let state = 'running';
  let headline = '';
  let sub = '';          // short subtitle (non-technical)
  let snippetBlock = ''; // optional <pre> with the raw CLI response
  /* Helper: format the stashed snippet for in-chip display.
   * Prefer a tidy pretty-printed JSON block when the snippet parses
   * as JSON, else show the raw text verbatim. Always shown in full —
   * no slicing or ellipsis — because the user explicitly asked for
   * "incomplete displays are not allowed". */
  const _formatSnippet = (raw) => {
    if (!raw) return '';
    let text = String(raw).trim();
    try {
      const trimmed = text.replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
      const parsed = JSON.parse(trimmed);
      text = JSON.stringify(parsed, null, 2);
    } catch (_e) { /* leave raw */ }
    return `<pre class="mp-snippet">${escapeHtml(text)}</pre>`;
  };
  if (phase === 'awaiting_approval') {
    headline = user
      ? `Waiting for mobile approval · ${user}`
      : 'Waiting for mobile approval';
    sub = 'Tap Approve on your mobile office app — push may take a few seconds to arrive';
  } else if (phase === 'approved') {
    state = 'done';
    icon = '✓';
    headline = user ? `Login approved · ${user}` : 'Login approved';
    sub = 'Session is live';
  } else if (phase === 'denied') {
    state = 'failed';
    icon = '✕';
    headline = 'Login denied';
    sub = 'Approval was rejected or cancelled on the phone';
    snippetBlock = _formatSnippet(lh.snippet);
  } else if (phase === 'timeout') {
    state = 'failed';
    icon = '⏰';
    headline = 'Login timed out';
    sub = 'No approval received in time — try again';
    snippetBlock = _formatSnippet(lh.snippet);
  } else {
    state = 'done';
    headline = 'Login finished';
    snippetBlock = _formatSnippet(lh.snippet);
  }
  return `<div class="mem-prefetch-chip mp-${state} mp-login-hint">` +
    `<span class="mp-icon">${icon}</span>` +
    `<span class="mp-text"><span class="mp-headline">${escapeHtml(headline)}</span>` +
    (sub ? `<span class="mp-sub">${escapeHtml(sub)}</span>` : '') +
    snippetBlock +
    `</span>` +
    (state === 'running'
      ? `<span class="mp-dots"><span>.</span><span>.</span><span>.</span></span>`
      : '') +
    `</div>`;
}

function renderMemoryPrefetchHtml(mp) {
  if (!mp) return "";
  const phase = mp.phase || "started";
  const selected = mp.selected || 0;
  const candidates = mp.candidates || 0;
  const bm25Ms = mp.bm25Ms || 0;
  const rerankMs = mp.rerankMs || 0;
  const totalMs = mp.totalMs || 0;
  const fellBack = !!mp.fellBack;

  // ── Build a short headline ──
  let icon = "🧠";
  let state = "running";
  let headline = "";
  let sub = "";
  if (phase === "started") {
    headline = "Surfacing relevant memories…";
    sub = (mp.totalMemories ? `${mp.totalMemories} total` : "") + " · BM25";
  } else if (phase === "bm25_done") {
    headline = `Filtering ${candidates} candidates with cheap model…`;
    sub = `BM25 ${bm25Ms}ms`;
  } else if (phase === "rerank_started") {
    headline = `Filtering ${candidates} candidates with cheap model…`;
    sub = `BM25 ${bm25Ms}ms`;
  } else if (phase === "done") {
    state = "done";
    if (selected === 0) {
      headline = "No prior memory relevant to this turn";
      sub = `${candidates||0} candidates · BM25 ${bm25Ms}ms · filter ${rerankMs}ms`;
    } else {
      headline = `Prefetched ${selected} memor${selected === 1 ? "y" : "ies"}`;
      const parts = [];
      if (candidates) parts.push(`${candidates} candidates`);
      parts.push(`BM25 ${bm25Ms}ms`);
      if (rerankMs) parts.push(`filter ${rerankMs}ms`);
      if (totalMs) parts.push(`total ${totalMs}ms`);
      if (fellBack) parts.push("⚠ fallback to BM25 top-3");
      sub = parts.join(" · ");
    }
  } else if (phase === "skipped") {
    state = "skipped";
    headline = "Memory prefetch skipped";
    sub = mp.reason || "";
  } else if (phase === "failed") {
    state = "failed";
    icon = "⚠️";
    headline = "Memory prefetch failed";
    sub = mp.reason || "";
  } else {
    headline = "Memory prefetch…";
    sub = phase;
  }

  // ── Optional detail panel listing picked memories ──
  let details = "";
  if (phase === "done" && selected > 0 && Array.isArray(mp.memories) && mp.memories.length > 0) {
    const items = mp.memories.map(m => {
      const nm = escapeHtml(m.name || "?");
      const sc = escapeHtml(m.scope || "");
      const ds = escapeHtml(m.description || "");
      return `<li><span class="mp-mem-name">${nm}</span>` +
             (sc ? ` <span class="mp-mem-scope">${sc}</span>` : "") +
             (ds ? `<div class="mp-mem-desc">${ds}</div>` : "") +
             `</li>`;
    }).join("");
    details = `<ul class="mp-mem-list">${items}</ul>`;
  }

  const expandable = !!details;
  return `<div class="mem-prefetch-chip mp-${state}${expandable ? ' mp-expandable' : ''}"${expandable ? ' onclick="this.classList.toggle(\'mp-expanded\')"' : ''}>` +
    `<span class="mp-icon">${icon}</span>` +
    `<span class="mp-text"><span class="mp-headline">${escapeHtml(headline)}</span>` +
    (sub ? `<span class="mp-sub">${escapeHtml(sub)}</span>` : "") +
    `</span>` +
    (state === "running" ? `<span class="mp-dots"><span>.</span><span>.</span><span>.</span></span>` : "") +
    (expandable ? `<span class="mp-chevron">▾</span>` : "") +
    (details ? `<div class="mp-details">${details}</div>` : "") +
    `</div>`;
}

function renderToolRoundsHTML(rounds, isStreaming) {
  if (!rounds || rounds.length === 0) return "";
  /* ★ UNIFIED: every round — tool calls AND swarm panels — goes into
   *   the single ptool-panel in chronological order. Swarm rounds
   *   render the full agent dashboard inline as a "row" so the user
   *   sees the order in which the main agent issued spawn_agents,
   *   await_agents, get_agent_result, and any other tools, all in
   *   one timeline. */
  return _renderUnifiedGroup(rounds);
}

/* ── Lazy thinking expand ────────────────────────────────
   Don't dump 30-100k+ chars of thinking text into the DOM
   on every render — inject it only when the user expands.
   This prevents DevTools / Elements tab from choking.      */

