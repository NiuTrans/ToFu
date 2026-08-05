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
    "inspect_image",
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
/* ★ The browser tool FAMILY — the single frontend list, kept in exact
 * parity with the backend sets (BROWSER_TOOL_NAMES |
 * LEGACY_BROWSER_TOOL_NAMES | ADVANCED_BROWSER_TOOL_NAMES |
 * PAGE_PREVIEW_TOOL_NAMES; guard: tests/test_browser_display_labels.py).
 * The v2 consolidation (pt_869e5648403e4745) added click/type/press_key/
 * menu_click/fill_form to the shipped surface but this list was never
 * updated — those rounds fell through to the generic lightning icon, a
 * name-mangled label and a spurious "✓ done" badge. Legacy names stay:
 * old conversations must keep rendering their browser cards. */
const _BROWSER_TOOL_FAMILY = [
  // v2 shipped surface
  "browser_list_tabs",
  "browser_read_page",
  "browser_execute_js",
  "browser_screenshot",
  "browser_click",
  "browser_type",
  "browser_press_key",
  "browser_navigate",
  "browser_close_tab",
  "browser_get_cookies",
  "browser_get_history",
  "browser_menu_click",
  "browser_fill_form",
  // Server-side preview (browser family, not extension-routed)
  "browser_preview_page",
  // Legacy names (removed from the model surface; history rendering)
  "browser_read_tab",
  "browser_get_interactive_elements",
  "browser_summarize_page",
  "browser_get_app_state",
  "browser_wait",
  "browser_hover",
  "browser_keyboard",
  "browser_create_tab",
  "browser_hover_and_click",
  "browser_right_click_menu",
];
function _isRoundBrowser(round) {
  return _BROWSER_TOOL_FAMILY.includes(round.toolName);
}
function _isRoundImageGen(round) {
  return round.toolName === "generate_image";
}
/* ★ Project-brain / conversation-reference tools. These return prose (a
   board listing, the charter text, a conversation digest, peer status) in
   `round.toolContent` plus a short `snippet`, but historically fell through
   to the bare generic tool line that shows only icon+name+badge — hiding all
   the actual content. `_isRoundConvMeta` routes them to a dedicated
   collapsible block that renders the full content as Markdown. */
const _CONV_META_TOOLS = new Set([
  "project_board_read", "project_board_post", "project_board_claim",
  "project_board_complete", "project_board_block",
  "project_charter_read", "project_charter_propose",
  // Withdrawn from the agent toolset 2026-07-30 (the charter is human-reviewed
  // — see lib/tools/conversation.py::CHARTER_TOOLS), but HISTORICAL rounds in
  // existing conversations still carry it and must keep rendering their content
  // instead of collapsing to a bare tool name.
  "project_charter_commit",
  "list_conversations", "get_conversation",
  "project_peer_status", "project_feed_read",
  "project_message", "project_intervene",
  "project_claim_path", "project_release_path",
  "project_commit",
]);
function _isRoundConvMeta(round) {
  return _CONV_META_TOOLS.has(round.toolName);
}
function _isRoundSwarm(round) {
  /* Only treat as swarm if the backend flagged it AND there's real swarm content */
  if (!round._swarm) return false;
  /* Must have at least one agent OR meaningful results to render the swarm panel.
     Even during active spawning, we don't show the panel until agents arrive.
     A durable `_swarmSnapshot` (persisted by the backend when the swarm settles)
     also counts — it's what makes a reloaded fire-and-forget swarm renderable
     even when the live `_swarmAgents` array and any `results` are absent. */
  if (!round._swarmAgents?.length
      && !round.results?.length
      && !(round._swarmSnapshot && round._swarmSnapshot.agents?.length)) return false;
  return true;
}

/* ★ Tool display metadata — icon, label, color for non-search/fetch tools */
const _TD_SVG = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px">${inner}</svg>`;
const _TOOL_DISPLAY = {
  web_search:    { icon: "", label: "Searching", color: "#60a5fa" },
  fetch_url:     { icon: "", label: "Fetching",  color: "#34d399" },
  spawn_agents:     { icon: "", label: "Swarm",          color: "#f59e0b" },
  await_agents:     { icon: "", label: "Awaiting Swarm", color: "#f59e0b" },
  get_agent_result: { icon: "", label: "Agent Result",   color: "#f59e0b" },
  create_memory:  { icon: "", label: "Memory",     color: "#a78bfa" },
  schedule_task: { icon: "", label: "Schedule",  color: "#fb923c" },
  timer_create:  { icon: _TD_SVG('<circle cx="12" cy="14" r="8"/><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/>'), label: "Timer Watcher", color: "#a855f7" },
  timer_manage:  { icon: _TD_SVG('<circle cx="12" cy="14" r="8"/><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/>'), label: "Timer",   color: "#a855f7" },
  bash_exec:     { icon: _TD_SVG('<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>'), label: "Running",   color: "#f472b6" },
  desktop_click: { icon: "", label: "Desktop",   color: "#94a3b8" },
  desktop_type:  { icon: _TD_SVG('<path d="M10 8h.01"/><path d="M12 12h.01"/><path d="M14 8h.01"/><path d="M16 12h.01"/><path d="M18 8h.01"/><path d="M6 8h.01"/><path d="M7 16h10"/><path d="M8 12h.01"/><rect width="20" height="16" x="2" y="4" rx="2"/>'), label: "Desktop",   color: "#94a3b8" },
  desktop_screenshot: { icon: "", label: "Desktop", color: "#94a3b8" },
  generate_image: { icon: "", label: "Image", color: "#e879f9" },
  ask_human: { icon: "", label: "Guidance", color: "#a5b4fc" },
  todo_write: { icon: "", label: "Checklist", color: "#34d399" },
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
  return { icon: _TD_SVG('<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>'), label: name.charAt(0).toUpperCase() + name.slice(1), color: "#94a3b8" };
}

function _getRoundBlockClass(round) {
  if (_isRoundFetch(round)) return "fetch-block";
  return "";
}
function _getRoundIcon(round) {
  if (_isRoundProject(round)) {
    const m = {
      read_files: "file",
      inspect_image: "zoomimg",
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
      browser_read_page: "read",
      browser_execute_js: "js",
      browser_screenshot: "screenshot",
      browser_get_cookies: "cookie",
      browser_get_history: "history",
      browser_create_tab: "newtab",
      browser_close_tab: "close",
      browser_navigate: "navigate",
      browser_preview_page: "screenshot",
      browser_click: "click",
      browser_type: "type",
      browser_press_key: "key",
      browser_menu_click: "menu",
      browser_fill_form: "form",
      browser_get_interactive_elements: "elements",
      browser_summarize_page: "read",
      browser_get_app_state: "js",
      browser_wait: "wait",
      browser_hover: "click",
      browser_keyboard: "key",
      browser_hover_and_click: "click",
      browser_right_click_menu: "menu",
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
         <div class="code-exec-header"><span class="code-exec-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg></span><span class="code-exec-label">Running...</span><span class="ptool-spinner"></span></div>
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
       <div class="code-exec-header"><span class="code-exec-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg></span><span class="code-exec-label">Code Execution</span><span class="code-exec-status">${statusLabel}</span></div>
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
  zoomimg:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>',
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
  click:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l7.07 16.97 2.51-7.39 7.39-2.51L4 4z"/><path d="M13.5 13.5 19 19"/></svg>',
  type:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/></svg>',
  key:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01"/><path d="M10 10h.01"/><path d="M14 10h.01"/><path d="M18 10h.01"/><path d="M7 14h10"/></svg>',
  menu:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>',
  form:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="3" width="14" height="18" rx="2"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="9" y1="12" x2="15" y2="12"/><line x1="9" y1="16" x2="13" y2="16"/></svg>',
  elements:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
  wait:
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>',
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
  // Conversation-reference tools — message bubble (get) / list (search).
  // Backend no longer prepends 💬/📋 to the label; these SVGs are the icon.
  get_conversation: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22z"/></svg>',
  list_conversations: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><line x1="8" y1="9" x2="16" y2="9"/><line x1="8" y1="13" x2="13" y2="13"/></svg>',
  // Project-brain tools — board (kanban columns), charter (compass/north-star),
  // peer (linked people). Inline SVG per the icon convention (§3.4).
  project_board_read: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="18" rx="1"/><rect x="14" y="3" width="7" height="11" rx="1"/></svg>',
  project_charter_read: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>',
  project_peer_status: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  // Activity feed — a pulse/heartbeat line (the live cross-conversation stream).
  project_feed_read: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  // Git commit (project_commit) — the standard git-commit mark: a center circle
  // on a horizontal line. Inline SVG per the icon convention (§3.4).
  project_commit: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><line x1="3" y1="12" x2="9" y2="12"/><line x1="15" y1="12" x2="21" y2="12"/></svg>',
  // Async-swarm bookkeeping tools (spawn_agents gets the full panel instead).
  await_agents: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  get_agent_result: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
  // MCP bridge tools (mcp__server__tool) — a plug glyph.
  mcp: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22v-5"/><path d="M15 8V2"/><path d="M17 8a1 1 0 0 1 1 1v4a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1z"/><path d="M9 8V2"/></svg>',
  context_compact: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>',
  ask_human: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  // Structured task checklist (todo_write) — a clipboard with a check.
  todo_write: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2h6a1 1 0 0 1 1 1v2H8V3a1 1 0 0 1 1-1z"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 13 2 2 4-4"/></svg>',
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
  // MCP bridge tools are named ``mcp__server__tool`` — collapse to the plug icon.
  if ((round.toolName || "").startsWith("mcp__")) return _webToolSvg.mcp;
  // Project-brain sibling tools share their family icon (post/claim/complete/
  // block → board; propose → charter; message/intervene → peer).
  if (_isRoundConvMeta(round)) {
    const tn = round.toolName || "";
    if (tn.startsWith("project_board_")) return _webToolSvg.project_board_read;
    if (tn.startsWith("project_charter_")) return _webToolSvg.project_charter_read;
    if (tn === "project_feed_read") return _webToolSvg.project_feed_read;
    if (tn === "project_message" || tn === "project_intervene") return _webToolSvg.project_peer_status;
    if (tn === "project_claim_path" || tn === "project_release_path") return _webToolSvg.project_board_read;
    if (tn === "project_commit") return _webToolSvg.project_commit;
  }
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
    ? `<div class="hg-translating-indicator"><span class="hg-spinner"></span> ${escapeHtml(t('project.hgTranslatingQuestion'))}</div>`
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
                placeholder="${escapeHtml(t('project.hgTextareaPlaceholder'))}"
                onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();submitHumanGuidanceFreeText('${gid}')}"></textarea>
      <div class="hg-freetext-actions">
        <button class="hg-submit-btn" onclick="event.stopPropagation();submitHumanGuidanceFreeText('${gid}')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          ${escapeHtml(t('project.hgSubmit'))}
        </button>
      </div>
    </div>`;
  }

  return `<div class="hg-card" data-gid="${gid}">
    <div class="hg-header">
      
      <span class="hg-title">${escapeHtml(t('project.hgPanelTitle'))}</span>
      <span class="hg-badge">${escapeHtml(t('project.hgWaitingReply'))}</span>
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
function _renderToolRootPill(round, noColon) {
  if (!round || !round._toolRoot) return "";
  const _ps = (typeof projectState !== "undefined") ? projectState : null;
  const _extrasCount = (_ps && Array.isArray(_ps.extraRoots)) ? _ps.extraRoots.length : 0;
  if (_extrasCount === 0) return "";
  const _sep = noColon ? "" : ":";
  return `<span class="ptool-root" title="Workspace root">${escapeHtml(round._toolRoot)}${_sep}</span>`;
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
  return `<span class="ptool-badge ${cls}" title="${tip}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/></svg> ${label}</span>`;
}

/**
 * Render a rejected hallucinated tool call. The backend (lib/tasks_pkg/
 * tool_dispatch.py) detected that `round.toolName` is not a real tool this
 * turn and rejected it WITHOUT executing — stamping `status:'rejected'` and a
 * `_rejected = {attempted, suggestions}` descriptor (mirrored onto the result
 * meta). We render the attempted name struck-through with a distinct "not a
 * real tool" badge and, when available, a "did you mean …" suggestion chip.
 */
function _renderRejectedToolLine(round, svg) {
  const meta = (round.results || [])[0] || {};
  const rej = round._rejected || meta.rejected || {};
  const attempted = escapeHtml(rej.attempted || round.toolName || "?");
  const sugg = Array.isArray(rej.suggestions) ? rej.suggestions.filter(Boolean) : [];
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const badgeLabel = escapeHtml(_t("tool.hallucinated", "not a real tool"));
  const tip = escapeHtml(_t(
    "tool.hallucinatedTip",
    "The model called a tool that doesn't exist this turn — it was rejected and never run."
  ));
  let suggHtml = "";
  if (sugg.length) {
    const chips = sugg.map((s) => `<code class="ptool-reject-sugg">${escapeHtml(s)}</code>`).join(" ");
    const did = escapeHtml(_t("tool.didYouMean", "did you mean"));
    suggHtml = `<span class="ptool-reject-hint">${did} ${chips}?</span>`;
  }
  /* SVG glyph (§3.4 — no emoji): a circle-slash "forbidden" mark. */
  const banSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><circle cx="12" cy="12" r="9"/><line x1="5.6" y1="5.6" x2="18.4" y2="18.4"/></svg>`;
  return `<div class="ptool-line ptool-rejected" title="${tip}">
       <span class="ptool-icon">${svg}</span>
       <span class="ptool-text ptool-reject-name">${attempted}</span>
       <span class="ptool-badge ptool-badge-reject">${banSvg} ${badgeLabel}</span>
       ${suggHtml}
     </div>`;
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
/* Parse a raw <swarm-update> / <task-notification> XML payload into a flat
 * field map so the human view can render clean fields + a Markdown preview
 * instead of dumping angle-bracket soup. Returns null when the text isn't a
 * recognizable payload (then we fall back to showing it verbatim). Values are
 * un-escaped from the backend's minimal XML escaping (&amp; &lt; &gt;). */
function _parseSwarmUpdateXml(text) {
  const raw = String(text == null ? "" : text);
  if (!/<swarm-update>|<task-notification>/.test(raw)) return null;
  const unesc = (s) => String(s == null ? "" : s)
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  const pick = (tag) => {
    const m = raw.match(new RegExp("<" + tag + ">([\\s\\S]*?)</" + tag + ">"));
    return m ? unesc(m[1]).trim() : "";
  };
  const rem = raw.match(/<remaining\s+running="(\d+)"\s+pending="(\d+)"\s*\/>/);
  return {
    agentId: pick("agent-id"),
    role: pick("role"),
    status: pick("status"),
    elapsed: pick("elapsed-seconds"),
    tokens: pick("tokens"),
    outputFile: pick("output-file"),
    error: pick("error"),
    preview: pick("preview"),
    running: rem ? +rem[1] : null,
    pending: rem ? +rem[2] : null,
  };
}

/* Status → chip class. "completed"/"done" → ok, "failed"/"error" → err,
 * everything else neutral. */
function _swarmStatusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "completed" || s === "done" || s === "success") return "ptool-badge-ok";
  if (s === "failed" || s === "error") return "ptool-badge-err";
  return "ptool-badge-info";
}

/* Render one beautified sub-agent update card from a parsed field map `f`.
 * Human view = agent/role header + status badge + elapsed/tokens meta
 * + the preview rendered as Markdown; nothing angle-bracketed on screen. */
function _renderSwarmUpdateCard(f) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const aid = escapeHtml(f.agentId || "");
  const role = f.role ? `<span class="sw-card-role">${escapeHtml(f.role)}</span>` : "";
  const stCls = _swarmStatusClass(f.status);
  const stChip = f.status
    ? `<span class="ptool-badge ${stCls} sw-card-status">${escapeHtml(f.status)}</span>` : "";
  const metaBits = [];
  if (f.elapsed) metaBits.push(escapeHtml(f.elapsed) + "s");
  if (f.tokens) metaBits.push(escapeHtml(f.tokens) + " tok");
  if (f.running != null && (f.running || f.pending))
    metaBits.push(_t("swarmCard.remaining", "{r} running · {p} pending")
      .replace("{r}", f.running).replace("{p}", f.pending));
  const metaHtml = metaBits.length
    ? `<span class="sw-card-meta">${metaBits.join(" · ")}</span>` : "";
  const errHtml = f.error
    ? `<div class="sw-card-error">${escapeHtml(f.error)}</div>` : "";
  const previewHtml = f.preview
    ? `<div class="sw-card-preview md-content">${(typeof renderMarkdown === "function") ? renderMarkdown(f.preview) : escapeHtml(f.preview)}</div>`
    : "";
  const fileHtml = f.outputFile
    ? `<div class="sw-card-file" title="${escapeHtml(f.outputFile)}">${Icon("file", 11)}<span>${escapeHtml(f.outputFile)}</span></div>`
    : "";
  return `<div class="sw-card">
       <div class="sw-card-head">
         ${aid ? `<span class="sw-card-agent">${aid}</span>` : ""}
         ${role}
         ${stChip}
         ${metaHtml}
       </div>
       ${errHtml}
       ${previewHtml}
       ${fileHtml}
     </div>`;
}

/* Header attribution for a peer-inject row: one title bubble per DISTINCT
 * sender conversation (never a bare id list). Shows up to `max` bubbles, then a
 * `+K` overflow chip — so multi-sibling injections (this project's normal case)
 * still read as titles, not `[sib1, sib2 …]`. */
function _peerFromBubbleGroup(previews, max) {
  const seen = new Set();
  const ids = [];
  (Array.isArray(previews) ? previews : []).forEach((p) => {
    const id = p && p.fromConv ? String(p.fromConv) : "";
    if (id && !seen.has(id)) { seen.add(id); ids.push(id); }
  });
  if (!ids.length) return "";
  const cap = max || 3;
  const shown = ids.slice(0, cap).map(_peerFromBubble).join("");
  const overflow = ids.length > cap
    ? `<span class="sw-peer-from-more">+${ids.length - cap}</span>`
    : "";
  return `<span class="sw-peer-from-group">${shown}${overflow}</span>`;
}

/* A small "who sent this" bubble: resolves a sibling conversation id to its
 * human-readable TITLE via the shared `convTitleById` seam (never a bare id —
 * falls back to a localized label). Rendered as a BUTTON carrying
 * `data-conv-jump` — clicking jumps to the source conversation (delegated
 * handler below resolves the full id via `convFullIdById` and calls
 * `loadConversation`). The tooltip carries the FULL title (the visible label
 * ellipsizes) + the raw id + the jump hint. */
function _peerFromBubble(cid) {
  const id = String(cid || "");
  if (!id) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const title = (typeof convTitleById === "function")
    ? (convTitleById(id) || id)
    : id;
  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  const jumpIcon = '<svg class="sw-peer-jump" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:.85em;height:.85em"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>';
  const tip = title + " · conv " + id + " — " + _t("peer.jumpToConv", "Open conversation");
  return `<button type="button" class="sw-peer-from-bubble" data-conv-jump="${escapeHtml(id)}" title="${escapeHtml(tip)}">${icon}<span>${escapeHtml(title)}</span>${jumpIcon}</button>`;
}

function _renderInboxInjectRow(round) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const count = round.inboxCount || (round.inboxPreviews || []).length || 0;
  const ids = (round.inboxAgentIds || []).filter(Boolean);
  const previews = Array.isArray(round.inboxPreviews) ? round.inboxPreviews : [];
  const idsLabel = ids.length
    ? `<span class="sw-inbox-row-ids">[${ids.slice(0, 4).map(escapeHtml).join(", ")}${ids.length > 4 ? ` +${ids.length - 4}` : ""}]</span>`
    : "";
  const word = count === 1
    ? _t("swarmCard.updateOne", "sub-agent update")
    : _t("swarmCard.updateMany", "sub-agent updates");
  const bodyHtml = previews.length
    ? previews.map(p => {
        const parsed = _parseSwarmUpdateXml(p.text || "");
        if (parsed) {
          // Backend may not have carried <agent-id> inside the payload — fall
          // back to the sibling `p.agentId` field so the card is never faceless.
          if (!parsed.agentId && p.agentId) parsed.agentId = p.agentId;
          return _renderSwarmUpdateCard(parsed);
        }
        // Unrecognized payload — show it verbatim (still the model's view).
        const aid = escapeHtml(p.agentId || "");
        return `<div class="sw-card sw-card-rawonly">` +
          (aid ? `<div class="sw-card-head"><span class="sw-card-agent">${aid}</span></div>` : "") +
          `<pre class="sw-card-raw-pre">${escapeHtml(p.text || "")}</pre>` +
        `</div>`;
      }).join("")
    : `<div class="sw-inbox-row-empty">${escapeHtml(_t("swarmCard.noPayload", "No payload available."))}</div>`;
  const badge = _t("peer.injectRowBadge", "injected → context");
  const label = _t("swarmCard.received", "Received");
  return `<details class="sw-inbox-row" data-rn="${round.roundNum}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg></span>
         <span class="ptool-text">${escapeHtml(label)} <b>${count}</b> ${escapeHtml(word)}</span>
         ${idsLabel}
         <span class="ptool-badge ptool-badge-info">${escapeHtml(badge)}</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
}


/* ── Peer-message inbox-injection row (Pillar #6 fast-path) ──
 * Renders a synthetic toolRound (flagged `_peerInject`) marking the moment the
 * orchestrator drained a peer message from a sibling conversation and injected
 * it as a user message before the next LLM round — the round-boundary fast
 * path (never mid-stream). Distinct from the queue-lane case, which renders as
 * a persisted .peer-msg-banner user bubble. Collapsible: the header shows
 * "Received N peer message(s)"; the body shows each sender + the message text
 * the model actually saw. */
function _renderPeerInjectRow(round) {
  const previews = Array.isArray(round.peerPreviews) ? round.peerPreviews : [];
  const count = round.peerCount || previews.length || 0;
  const word = count === 1
    ? (typeof t === "function" ? t("peer.injectRowOne") : "peer message")
    : (typeof t === "function" ? t("peer.injectRowMany") : "peer messages");
  const label = typeof t === "function" ? t("peer.injectRowLabel") : "Received";
  const badge = typeof t === "function" ? t("peer.injectRowBadge") : "injected → context";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  /* Sender attribution dedup: the row header ALREADY carries one title bubble
   * per distinct sender, so when every preview came from the SAME conversation
   * (the common case) the body cards must NOT repeat the identical bubble —
   * that repetition read as a rendering bug. Per-card attribution is kept only
   * for a genuinely multi-sender injection. */
  const _distinctSenders = new Set(
    previews.map(p => (p && p.fromConv) ? String(p.fromConv) : "").filter(Boolean));
  const _perCardAttribution = _distinctSenders.size > 1;
  const bodyHtml = previews.length
    ? previews.map(p => {
        const text = String(p.text == null ? "" : p.text);
        // Attribution reads as a conversation-title bubble (via convTitleById),
        // NOT a raw id — users care who sent it. Peer messages are plain prose,
        // rendered as Markdown for the human view.
        const fromBubble = (_perCardAttribution && p.fromConv) ? _peerFromBubble(p.fromConv) : "";
        const bodyMd = text.trim()
          ? `<div class="sw-card-preview md-content">${(typeof renderMarkdown === "function") ? renderMarkdown(text) : escapeHtml(text)}</div>`
          : "";
        return `<div class="sw-card sw-peer-card-item">` +
          (fromBubble ? `<div class="sw-card-head">${fromBubble}</div>` : "") +
          bodyMd +
        `</div>`;
      }).join("")
    : `<div class="sw-inbox-row-empty">${escapeHtml(_t("peerCard.noPayload", "No message available."))}</div>`;
  // Header "sender" attribution: one TITLE bubble per distinct sender (up to 3,
  // then a +K overflow chip). NEVER a raw id list — users care who sent it, and
  // multi-sibling injection is this project's normal case.
  const headBubble = _peerFromBubbleGroup(previews, 3);
  return `<details class="sw-inbox-row sw-peer-row" data-rn="${round.roundNum}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>
         <span class="ptool-text">${escapeHtml(label)} <b>${count}</b> ${escapeHtml(word)}</span>
         ${headBubble}
         <span class="ptool-badge ptool-badge-info">${escapeHtml(badge)}</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
}

/* ── Human-steer inbox-injection row (mid-turn operator interjection) ──
 * Renders a synthetic toolRound (flagged `_userSteerInject`) marking the moment
 * the orchestrator drained a HUMAN "steer" message the operator sent while this
 * turn was still generating (composer inject-mode = steer) and injected it as a
 * user message before the next LLM round. Distinct from a sibling peer message
 * (_peerInject) and a sub-agent result (_inboxInject): this is the operator
 * talking to their own running turn. Collapsible: header shows "You steered N
 * time(s)"; body shows each steer message the model actually saw. */
function _renderUserSteerInjectRow(round) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const previews = Array.isArray(round.steerPreviews) ? round.steerPreviews : [];
  const count = round.steerCount || previews.length || 0;
  const word = count === 1
    ? _t("steer.injectRowOne", "steer message")
    : _t("steer.injectRowMany", "steer messages");
  const label = _t("steer.injectRowLabel", "You steered mid-turn");
  const badge = _t("peer.injectRowBadge", "injected → context");
  const bodyHtml = previews.length
    ? previews.map(p => {
        const text = String(p.text == null ? "" : p.text);
        const bodyMd = text.trim()
          ? `<div class="sw-card-preview md-content">${(typeof renderMarkdown === "function") ? renderMarkdown(text) : escapeHtml(text)}</div>`
          : "";
        return `<div class="sw-card sw-steer-card-item">` + bodyMd + `</div>`;
      }).join("")
    : `<div class="sw-inbox-row-empty">${escapeHtml(_t("steer.noPayload", "No message available."))}</div>`;
  return `<details class="sw-inbox-row sw-steer-row" data-rn="${round.roundNum}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></span>
         <span class="ptool-text">${escapeHtml(label)} <b>${count}</b> ${escapeHtml(word)}</span>
         <span class="ptool-badge ptool-badge-info">${escapeHtml(badge)}</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
}

/* ── Intent-stall nudge row (the loop re-driving a stalled model) ──
 * Renders a synthetic toolRound (flagged `_stallNudge`) marking the moment the
 * orchestrator detected an INTENT STALL — a tool call that did not run,
 * followed by a prose-only round with no tool calls ("I'll do X" then nothing)
 * — and injected a system instruction to re-drive the model.
 *
 * Distinct from the other three inject lanes in WHO wrote the injected text:
 * swarm (`_inboxInject`) carries a sub-agent's result, peer (`_peerInject`)
 * another conversation's message, steer (`_userSteerInject`) the operator's own
 * words. This one is written by the LOOP ITSELF. That is exactly why it must be
 * legible as a system action — a system-authored `role='user'` message must
 * never read as something the user said.
 *
 * The header states the bound ("at most once per turn") because otherwise
 * "the system re-drove my agent" reads as an unbounded spend risk; the body
 * names the tool that failed and shows the VERBATIM instruction that was sent,
 * so the user can judge the intervention instead of trusting a paraphrase. */
function _renderStallNudgeRow(round) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const label = _t("stall.injectRowLabel", "Nudged the model to continue");
  const badge = _t("peer.injectRowBadge", "injected → context");
  const tool = round.stallTool || "";
  const reason = tool
    ? _t("stall.reasonWithTool", "`{tool}` did not run, and the next round was "
         + "text only — the model said what it would do, then stopped.")
        .replace("{tool}", tool)
    : _t("stall.reasonGeneric", "The previous tool call did not run, and the "
         + "next round was text only.");
  const boundTxt = _t("stall.bound", "At most once per turn — if the model "
                      + "stalls again it is allowed to stop.");
  const promptHtml = round.stallPrompt
    ? `<div class="sw-card sw-stall-card-item">`
      + `<div class="sw-card-head"><span class="sw-card-role">`
      + `${escapeHtml(_t("stall.promptLabel", "Sent to the model"))}</span></div>`
      + `<pre class="sw-card-raw-pre">${escapeHtml(round.stallPrompt)}</pre></div>`
    : "";
  return `<details class="sw-inbox-row sw-stall-row" data-rn="${round.roundNum}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M3 2v6h6"/><path d="M3 8a9 9 0 1 0 3-5.7L3 8"/></svg></span>
         <span class="ptool-text">${escapeHtml(label)}</span>
         <span class="ptool-badge ptool-badge-info">${escapeHtml(badge)}</span>
       </summary>
       <div class="sw-inbox-row-body">
         <div class="sw-stall-reason">${escapeHtml(reason)}</div>
         <div class="sw-stall-bound">${escapeHtml(boundTxt)}</div>
         ${promptHtml}
       </div>
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
    const safeUrl = /^https?:\/\//i.test(url) ? url : "";
    const titleHtml = safeUrl
      ? `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener">${title}</a>`
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
         ${_rowRightControls(round)}
       </summary>
       <div class="ptool-memory-body">${inner}</div>
     </details>`;
}

/* ★ Checklist block (todo_write) — a collapsible progress card rendered off
   the STRUCTURED `meta.todos` the backend attaches (extra={'todos': todos} in
   handlers/misc.py), never re-parsed from the result prose.

   Design: reads like every other tool row — collapsed by default, same
   monospace `.ptool-text` label + `.ptool-icon`. The header carries an
   at-a-glance progress WITHOUT expanding: a slim inline mini-bar + a done/total
   count chip (turns green at 100%) + the in-progress item's text as a subtle
   "current step" preview. Expanded, the body is a vertical-timeline stepper:
   a connector line threads the state glyphs (✓ done / ◔ in-progress / ○
   pending), the in-progress step is highlighted (that's what's happening now)
   and completed steps are struck through. */
function _renderTodoBlock(round, svg, q, badgeHtml) {
  const meta = (round.results || [])[0] || {};
  let todos = Array.isArray(meta.todos) ? meta.todos : null;
  // Fallback: some persisted/legacy rounds may carry the list on the round
  // itself rather than in the first result meta.
  if (!todos && Array.isArray(round.todos)) todos = round.todos;
  if (!todos) return null;

  const _t = (typeof t === "function") ? t : (k, d) => d;
  const total = todos.length;
  const done = todos.filter((x) => x && x.status === "completed").length;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const allDone = total > 0 && done === total;

  const headLabel = total
    ? _t("todo.head", "Checklist")
    : _t("todo.cleared", "Checklist cleared");

  const _ICON = {
    completed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    in_progress: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 3a9 9 0 1 0 9 9A9 9 0 0 0 12 3zm0 2v7l4.5 2.6A7 7 0 0 1 12 5z"/></svg>',
    pending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/></svg>',
  };

  let rows = "";
  for (const item of todos) {
    if (!item || typeof item !== "object") continue;
    const st = (item.status === "completed" || item.status === "in_progress") ? item.status : "pending";
    const text = escapeHtml(String(item.content || "").trim());
    rows += `<div class="ptool-todo-item ptool-todo-${st}">` +
      `<span class="ptool-todo-mark">${_ICON[st]}</span>` +
      `<span class="ptool-todo-text">${text}</span>` +
      `</div>`;
  }
  if (!rows) {
    rows = `<div class="ptool-todo-empty">${escapeHtml(_t("todo.emptyBody", "No checklist items."))}</div>`;
  }

  const barHtml = total
    ? `<span class="ptool-todo-minibar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" title="${done}/${total}">` +
        `<span class="ptool-todo-minibar-fill${allDone ? " ptool-todo-minibar-done" : ""}" style="width:${pct}%"></span>` +
      `</span>`
    : "";
  const countChip = total
    ? `<span class="ptool-todo-count${allDone ? " ptool-todo-count-done" : ""}">${done}/${total}</span>`
    : "";

  return `<details class="ptool-todo-block" data-rn="${round.roundNum}">
       <summary class="ptool-line ptool-todo-header">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${escapeHtml(headLabel)}</span>
         ${barHtml}
         ${countChip}
         ${_rowRightControls(round)}
       </summary>
       <div class="ptool-todo-body"><div class="ptool-todo-list">${rows}</div></div>
     </details>`;
}

/* ── MOVED to ui/tool_rounds_rich.js (_DEFERRED_FILES, Epic-E pt_3879f00e sub-4, 2026-08-01) — rich conv-meta + timer-watcher renderers; core dispatch is typeof-guarded and degrades to the generic ptool-line until the feature bundle lands. ── */

/* Localize the inspect_image ops chip. The backend (lib/file_reader.py) builds
   an English, LLM-facing op string like "cropped, zoom 2×" / "rotated 90°,
   fit to 4000px" / "full frame". Here we translate the human-facing chip at
   render time (keeping the dynamic numbers). `mode === "title"` returns the
   tooltip label instead of the chip body. */
function _localizeInspectOps(tFn, ops, mode) {
  const _t = (typeof tFn === "function") ? tFn : (k, d) => d;
  if (mode === "title") return _t("inspect.opsTitle", "Applied transform");
  const raw = String(ops || "").trim();
  if (!raw) return "";
  if (raw === "full frame") return _t("inspect.fullFrame", "full frame");
  return raw.split(",").map((seg) => {
    const s = seg.trim();
    if (s === "cropped") return _t("inspect.cropped", "cropped");
    if (s === "grid overlay") return _t("inspect.gridOverlay", "grid overlay");
    let m;
    if ((m = s.match(/^rotated\s+(.+)$/)))
      return _t("inspect.rotated", "rotated {deg}").replace("{deg}", m[1]);
    if ((m = s.match(/^zoom\s+(.+)$/)))
      return _t("inspect.zoom", "zoom {factor}").replace("{factor}", m[1]);
    if ((m = s.match(/^fit to\s+(.+)$/)))
      return _t("inspect.fitTo", "fit to {size}").replace("{size}", m[1]);
    return s;  // unknown token — pass through verbatim
  }).join(_t("inspect.opsSep", ", "));
}

/* ── MCP resource linkifier ───────────────────────────────────────────
 * The backend attaches `round._mcpLinks` = {label → href} for any MCP
 * tool call whose resource resolves to a URL (e.g. an Overleaf project).
 * The label is the EXACT substring `_mcp_arg_suffix` rendered on the
 * title line — a human-readable project name when cached, else the
 * `6a1e7…a668` short-id. We wrap that substring in an <a> so users can
 * jump straight to the project instead of staring at an unreadable id.
 *
 * `text` is ALREADY HTML-escaped; labels are escaped the same way before
 * matching so a name with special chars still lines up. We only replace
 * the first occurrence and skip if the label is empty or already inside
 * an anchor (defensive). */
function _linkifyMcpLabels(text, round) {
  const links = round && round._mcpLinks;
  if (!links || typeof links !== "object" || !text) return text;
  let out = text;
  for (const label of Object.keys(links)) {
    const href = links[label];
    if (!label || !href) continue;
    // Only allow http(s) hrefs — never inject javascript:/data: URLs.
    if (!/^https?:\/\//i.test(href)) continue;
    const escLabel = escapeHtml(label);
    const idx = out.indexOf(escLabel);
    if (idx === -1) continue;
    const anchor = `<a class="ptool-mcp-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(href)}">${escLabel}</a>`;
    out = out.slice(0, idx) + anchor + out.slice(idx + escLabel.length);
  }
  return out;
}

/* Recovery-rebuilt rounds can carry NO `query` at all: boot crash recovery
 * rebuilds toolRounds from the persisted segments (a wire-replay view — only
 * toolCallId/toolName/toolArgs/toolContent/status/llmRound) and, for history
 * written before the display projection landed, persisted them as-is. The
 * generic line below interpolates `q` as the whole title, so a query-less
 * round rendered as an EMPTY card (an icon and nothing else —
 * the ms1auj3n restart symptom). Never render blank: fall back to the tool
 * label plus a short first-string-arg summary so the row still says what ran. */
function _recoveryRoundFallbackTitle(round, td) {
  const base = (td && td.label) || round.toolName || 'tool';
  let summary = '';
  try {
    const args = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs;
    if (args && typeof args === 'object') {
      for (const k of Object.keys(args)) {
        const v = args[k];
        if (typeof v === 'string' && v.trim()) { summary = v.trim().split('\n')[0].slice(0, 80); break; }
      }
    }
  } catch (e) { /* malformed toolArgs — the label alone still beats a blank row */ }
  return escapeHtml(summary ? base + ' — ' + summary : base);
}

function _renderUnifiedToolLine(round, isSearching) {
  const svg = _getToolSvg(round);
  const td = _getToolDisplay(round);
  /* Preserve real newlines in the tool-call title — batch search/fetch
   * displays render one item per line so users can see every candidate
   * without elision. escapeHtml first (HTML-safe), THEN substitute
   * \n → <br> so the browser actually breaks the line. */
  const q = round.query
    ? _linkifyMcpLabels(escapeHtml(round.query).replace(/\n/g, '<br>'), round)
    : _recoveryRoundFallbackTitle(round, td);
  const results = round.results || [];
  const meta = results[0] || {};
  const rootPill = _renderToolRootPill(round);
  /* run_command / code_exec render the root prefix inline on the title
   * line (alongside the description), where a trailing colon reads as if
   * it introduces the description — so the command-header variant drops it. */
  const cmdRootPill = _renderToolRootPill(round, true);
  /* ★ Harness self-repair badge — the backend auto-corrected this call's
   *   malformed arguments (truncated/invalid JSON, or schema-shape coercion).
   *   Surfacing it tells the user the displayed/executed args differ from
   *   the model's raw (broken) output. */
  const repairedBadge = _renderToolRepairedBadge(round);
  /* Shared locals handed to the per-branch renderer helpers below — built
   * once; each helper destructures only what it needs so the moved code
   * stays byte-identical to the pre-split inline branches. */
  const ctx = { svg, td, q, results, meta, rootPill, cmdRootPill, repairedBadge, isSearching };

  // ★ Async-swarm inbox injection — synthetic timeline row marking the point
  //   where the model received N <swarm-update> messages (sub-agent results)
  //   as a user message before its next turn. Not a real tool call, but
  //   rendered in chronological order so the user sees exactly when/what the
  //   model was handed. Collapsible: expands to the raw payloads.
  if (round._inboxInject) {
    return _renderInboxInjectRow(round);
  }

  if (round._peerInject) {
    return _renderPeerInjectRow(round);
  }

  if (round._userSteerInject) {
    return _renderUserSteerInjectRow(round);
  }

  if (round._stallNudge) {
    return _renderStallNudgeRow(round);
  }

  // ★ Hallucinated / rejected tool — the model invented a tool that does not
  //   exist this turn (e.g. `search_web` when only `web_search` is real).
  //   The backend classified + rejected it (status:'rejected' + _rejected),
  //   so it NEVER executed. Render it distinctly so the user can tell a fake
  //   tool call apart from a real one that errored.
  if (round.status === "rejected" && (round._rejected || (meta && meta.rejected))) {
    return _renderRejectedToolLine(round, svg);
  }

  // ★ Human Guidance — LLM is asking the user a question
  if (round.status === "awaiting_human" && round.guidanceId) {
    return _renderHumanGuidanceCard(round, svg);
  }

  // ★ Human Guidance — skipped (task ended before user answered) /
  //   submitted but not yet confirmed by server (tool_result pending)
  const hgRowHtml = _renderHumanGuidanceRows(round, ctx);
  if (hgRowHtml) return hgRowHtml;

  // ★ Pending approval state — show approve/reject buttons
  const approvalHtml = _renderPendingApprovalBlock(round, ctx);
  if (approvalHtml) return approvalHtml;

  // ★ Timer Watcher: render collapsible poll checks (rich renderer lives in
  //   DEFERRED ui/tool_rounds_rich.js — absence falls through to the generic
  //   line until the feature bundle lands; Epic-E sub-4)
  if (((round._timerPolls && round._timerPolls.length > 0) || round._timerSkipCount)
      && typeof _renderTimerWatcherBlock === 'function') {
    return _renderTimerWatcherBlock(round, svg);
  }
  // Timer tool with "searching" status but no polls yet — show initial waiting
  // After reconnection, backend now includes _timerPolls in state snapshots,
  // so this state should be brief (only before the first poll fires).
  const timerWaitHtml = _renderTimerWaitingRow(round, ctx);
  if (timerWaitHtml) return timerWaitHtml;

  // ★ Interactive stdin: subprocess is waiting for user keyboard input
  const stdinHtml = _renderStdinBlock(round, ctx);
  if (stdinHtml) return stdinHtml;

  // ★ Interrupted — the task was aborted (Stop) while this tool round was
  //   still in-flight. The backend dangling-round sweep
  //   (orchestrator._finalize_dangling_tool_rounds) stamps status='aborted'
  //   on rounds the abort short-circuit left in 'searching'. Render a static
  //   "interrupted" affordance — NO spinner — so it never shows "Running…"
  //   live or after reload. Real results (if any) fall through to the normal
  //   done renderers below, since the sweep only marks result-less rounds.
  const abortedHtml = _renderAbortedRow(round, ctx);
  if (abortedHtml) return abortedHtml;

  const searchingHtml = _renderSearchingRow(round, ctx);
  if (searchingHtml) return searchingHtml;

  // ★ run_command / code_exec: render as inline terminal block with collapsible output
  const cmdDoneHtml = _renderCmdDoneBlock(round, ctx);
  if (cmdDoneHtml) return cmdDoneHtml;

  // ★ browser_execute_js — render as an inline code block (JS in, result out),
  //   mirroring the run_command terminal block. A cramped one-line "12165686:
  //   (() => {…" row is unreadable; show the full snippet + collapsible result.
  const execJsHtml = _renderBrowserExecJsBlock(round, ctx);
  if (execJsHtml) return execJsHtml;

  // ★ Web search / fetch — descriptive 0-result reason row, or collapsible
  //   result list (per-query grouping, vertical cards, engine breakdown).
  const searchRowsHtml = _renderSearchRows(round, ctx);
  if (searchRowsHtml) return searchRowsHtml;

  // ★ read_files / inspect_image image(s): render inline thumbnails when the
  //   backend attached data URIs (meta.imageDataUris).
  const readImgHtml = _renderReadImagesBlock(round, ctx);
  if (readImgHtml) return readImgHtml;

  // ★ Image generation / editing: render inline image card.
  const imageGenHtml = _renderImageGenBlock(round, ctx);
  if (imageGenHtml) return imageGenHtml;

  // Determine badge
  const badgeHtml = _computeToolBadgeHtml(round, ctx);

  const compactionLabelHtml = _renderCompactionLabel(round);
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

  // ★ todo_write — collapsible checklist progress card (state glyphs + a slim
  //   progress bar), rendered off the structured meta.todos the backend
  //   attaches. Falls through to the generic line only if the list is absent.
  if (round.toolName === "todo_write") {
    const todoHtml = _renderTodoBlock(round, svg, q, badgeHtml);
    if (todoHtml) return todoHtml;
  }

  // ★ write_file — collapsible inline preview of the written content,
  //   mirroring the apply_diff expand-on-click block.
  const writeFileHtml = _renderWriteFileBlock(round, ctx, badgeHtml, compactionLabelHtml);
  if (writeFileHtml) return writeFileHtml;

  // ★ Single apply_diff / insert_content — collapsible inline diff
  const singleDiffHtml = _renderSingleDiffBlock(round, ctx, badgeHtml, compactionLabelHtml);
  if (singleDiffHtml) return singleDiffHtml;

  // ★ Batch edit tools (apply_diffs / insert_contents) — collapsible per-edit list
  const batchEditsHtml = _renderBatchEditsBlock(round, ctx, badgeHtml, compactionLabelHtml);
  if (batchEditsHtml) return batchEditsHtml;

  // ★ Project-brain / conversation-meta tools — render their full prose
  //   output in a collapsible Markdown card instead of the bare generic line
  //   (which hid all the content). Only when the round has settled (done);
  //   the in-flight "searching…" state is handled by the generic active
  //   branch above.
  if (_isRoundConvMeta(round) && round.status !== "rejected") {
    // typeof-guard: the rich renderer is DEFERRED (ui/tool_rounds_rich.js,
    // Epic-E sub-4); while it is in flight degrade to the generic line.
    const convMetaHtml = (typeof _renderConvMetaBlock === 'function')
      ? _renderConvMetaBlock(round, svg, q, badgeHtml) : "";
    if (convMetaHtml) return convMetaHtml;
  }

  return `<div class="ptool-line">
       <span class="ptool-icon">${svg}</span>
       ${compactionLabelHtml}
       ${rootPill}
       <span class="ptool-text">${q}</span>
       ${repairedBadge}
       ${badgeHtml}
       ${_rowRightControls(round)}
     </div>`;
}

/* ══════════════════════════════════════════════════════════════════════════
 * Per-branch renderers for _renderUnifiedToolLine. Each helper guards on its
 * own trigger condition and returns "" when the branch does not apply, so the
 * dispatcher stays a flat ordered list of `if (html) return html;` probes —
 * the probe ORDER is the render priority and must not change.
 * ══════════════════════════════════════════════════════════════════════════ */

// ★ Human Guidance — skipped (task ended before user answered) /
//   submitted but not yet confirmed by server (tool_result pending)
function _renderHumanGuidanceRows(round, ctx) {
  const { svg, td } = ctx;
  if (round.status === "done" && round.toolName === "ask_human" && round._hgSkipped) {
    const skippedQ = escapeHtml((round.guidanceQuestion || '').slice(0, 60));
    return `<div class="ptool-line hg-skipped-line">
      <span class="ptool-icon">${svg}</span>
      <span class="ptool-text">${td.label || 'Guidance'}${skippedQ ? ' — ' + skippedQ : ''}</span>
      <span class="ptool-badge ptool-badge-skip">${escapeHtml(t('project.hgUnanswered'))}</span>
    </div>`;
  }
  if (round.status === "submitted" && round.toolName === "ask_human") {
    const respPreview = escapeHtml((round._hgUserResponse || '').slice(0, 80));
    return `<div class="ptool-line hg-submitted-line">
      <span class="ptool-icon">${svg}</span>
      <span class="ptool-text">${td.label || 'Guidance'}${respPreview ? ' — ' + respPreview : ''}</span>
      <span class="ptool-badge ptool-badge-done">${escapeHtml(t('project.hgAnswered'))}</span>
      <span class="hg-submitted-spinner" title="${escapeHtml(t('project.hgWaitingContinue'))}"></span>
    </div>`;
  }
  return "";
}

// ★ Pending approval state — show approve/reject buttons
function _renderPendingApprovalBlock(round, ctx) {
  const { svg, q } = ctx;
  if (!(round.status === "pending_approval" && round.approvalId)) return "";
  const aid = escapeHtml(round.approvalId);
  const ameta = round.approvalMeta || {};
  let detailHtml = "";
  if (Array.isArray(ameta.riskFields) && ameta.riskFields.length) {
    // ★ Generic risk-field list — the shape EVERY write tool can use.
    //
    // The four shapes below (batch / diff / command / contentPreview) are
    // hardcoded per tool family, which is why a write tool outside those
    // families used to render NO detail block at all: the user saw a bare
    // tool name plus Approve/Reject and approved blind. Rather than invent a
    // fifth, sixth, … bespoke shape per family, an enricher now just declares
    // WHICH arguments carry the risk and this branch renders them uniformly.
    // Checked FIRST so an enricher can opt into it explicitly.
    const rows = ameta.riskFields
      .filter((f) => f && f.label != null)
      .map((f) => {
        const val = f.value == null ? "" : String(f.value);
        const shown = val.length > 2000 ? val.slice(0, 2000) + "…" : val;
        const lines = shown.split("\n");
        const body = lines
          .map(
            (l) =>
              `<div class="ptool-diff-line ptool-diff-add"><span class="ptool-diff-sign">+</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`,
          )
          .join("");
        return `<div class="ptool-risk-field"><div class="ptool-risk-label">${escapeHtml(String(f.label))}</div>${body}</div>`;
      })
      .join("");
    const noteHtml = ameta.description
      ? `<div class="ptool-cmd-desc">${escapeHtml(ameta.description)}</div>`
      : "";
    detailHtml = `<div class="ptool-diff-preview">${noteHtml}${rows}</div>`;
  } else if (ameta.batchMode && ameta.editSummaries) {
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
        diffLines += `<div class="ptool-diff-line ptool-diff-del"><span class="ptool-diff-sign">-</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
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
      diffLines += `<div class="ptool-diff-line ptool-diff-del"><span class="ptool-diff-sign">-</span><span class="ptool-diff-code">${escapeHtml(l)}</span></div>`;
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
    const cmdDescHtml = ameta.description
      ? `<div class="ptool-cmd-desc">${escapeHtml(ameta.description)}</div>`
      : "";
    detailHtml = `<div class="ptool-diff-preview">${cmdDescHtml}<pre class="ptool-cmd-code" style="margin:0;padding:8px 12px;font-size:12px;"><code>$ ${cmdText}</code></pre></div>`;
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

// Timer tool with "searching" status but no polls yet — show initial waiting
// After reconnection, backend now includes _timerPolls in state snapshots,
// so this state should be brief (only before the first poll fires).
function _renderTimerWaitingRow(round, ctx) {
  const { q } = ctx;
  if (!(round.toolName === "timer_create" && round.status === "searching" && !round._timerPolls)) return "";
  // ★ Try to recover timer polls from the API if timerId is known
  if (round._timerTimerId && !round._timerPollsRecoveryAttempted) {
    round._timerPollsRecoveryAttempted = true;
    _recoverTimerPolls(round);
  }
  return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">${Icon('timer')}</span>
         <span class="ptool-text">${q || escapeHtml(t('timerBlock.watcherTitle'))}</span>
         <span class="ptool-badge ptool-badge-warn">${escapeHtml(t('timerBlock.waitingFirstPoll'))}</span>
         <span class="ptool-spinner"></span>
       </div>`;
}

// ★ Interactive stdin: subprocess is waiting for user keyboard input
function _renderStdinBlock(round, ctx) {
  const { svg, rootPill } = ctx;
  if (!(round.status === "awaiting_stdin" && round.stdinId)) return "";
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

// ★ Interrupted — the task was aborted (Stop) while this tool round was
//   still in-flight. The backend dangling-round sweep
//   (orchestrator._finalize_dangling_tool_rounds) stamps status='aborted'
//   on rounds the abort short-circuit left in 'searching'. Render a static
//   "interrupted" affordance — NO spinner — so it never shows "Running…"
//   live or after reload. Real results (if any) fall through to the normal
//   done renderers below, since the sweep only marks result-less rounds.
function _renderAbortedRow(round, ctx) {
  const { svg, td, meta, rootPill } = ctx;
  if (!(round.status === "aborted" && !(round.results && round.results.length && !meta.interrupted))) return "";
  const cmdText = escapeHtml(round.query || meta.title || td.label || round.toolName || "");
  return `<div class="ptool-line ptool-interrupted" data-rn="${round.roundNum}">
         <span class="ptool-icon">${svg}</span>
         ${rootPill}
         <span class="ptool-text">${cmdText}</span>
         <span class="ptool-badge ptool-badge-interrupted">interrupted</span>
       </div>`;
}

/* ── Live run_command timer (pt_1a82ffb3) ────────────────────────────────
 * A long command showed `Running...` + a spinner and nothing else, so there
 * was no way to tell a 3-second command from a 30-minute one, nor how much of
 * an explicit `timeout` budget was left.
 *
 * Both clocks come from the SERVER and ride the round, which is what makes the
 * display survive a conversation switch / reload: `execStartTs` (subprocess
 * spawn) and `deadlineTs` (absolute kill time, already adjusted for the
 * cross-DC multiplier and the MAX_COMMAND_TIMEOUT clamp). We only ever
 * SUBTRACT from them. A client-side stopwatch cannot do this — it re-mints on
 * every paint and every reconnect, so a 20-minute-old command would render as
 * freshly started, which is precisely the bug this feature must not ship.
 *
 * `execStartTs` is preferred over `tStart` because tStart is the round ANNOUNCE
 * time: a write-approval gate can sit minutes before the process actually
 * starts, and counting that as execution would over-report. tStart is the
 * fallback for a round that predates this contract. */
function _cmdTimerAnchor(round) {
  if (!round) return null;
  const v = (round.execStartTs != null) ? round.execStartTs : round.tStart;
  return (typeof v === 'number' && v > 0) ? v : null;
}

/* Compact duration: 45s / 3m12s / 1h04m. */
function _fmtDur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return m + 'm' + String(rs).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') + 'm';
}

/* The chip's text + urgency class for a given wall clock. Shared by the first
 * paint and the 1 Hz ticker so the two can never disagree. */
function _cmdTimerState(round, nowMs) {
  const anchor = _cmdTimerAnchor(round);
  const deadline = (typeof round.deadlineTs === 'number' && round.deadlineTs > 0)
    ? round.deadlineTs : null;
  const _tf = (typeof t === 'function') ? t : (k, d) => d;
  if (deadline != null) {
    const left = deadline - nowMs;
    /* Past the deadline we do NOT show a negative number: the backend is
     * SIGKILLing the process tree and the terminal frame is on its way. Say
     * that, rather than counting into the negative or freezing at 0s. */
    if (left <= 0) {
      return { txt: _tf('toolTimer.terminating', 'terminating…'), cls: ' ptool-cmd-timer-over' };
    }
    return {
      txt: _tf('toolTimer.countdown', '{n} left').replace('{n}', _fmtDur(left)),
      cls: left <= 10000 ? ' ptool-cmd-timer-soon' : '',
    };
  }
  /* No deadline — the DEFAULT for run_command (no ceiling). Count UP, which is
   * the common case and the one that answers "how long has this been going?". */
  if (anchor == null) return null;
  return { txt: _fmtDur(nowMs - anchor), cls: '' };
}

function _renderCmdTimerChip(round) {
  const st = _cmdTimerState(round, Date.now());
  if (!st) return '';
  const anchor = _cmdTimerAnchor(round);
  const dl = (typeof round.deadlineTs === 'number' && round.deadlineTs > 0) ? round.deadlineTs : '';
  /* The data-* attributes are what the ticker updates in place — no re-render,
   * so the fingerprint gate in _syncToolRoundsDOM (which correctly skips when
   * no SSE event landed) cannot freeze the value. */
  return `<span class="ptool-cmd-timer${st.cls}" data-cmd-timer="1"`
    + ` data-cmd-anchor="${anchor == null ? '' : anchor}"`
    + ` data-cmd-deadline="${dl}">${escapeHtml(st.txt)}</span>`;
}

/* ── Per-command interrupt button (pt_232244fb) ──────────────────────────
 * The whole-task Stop button kills the TURN; this kills only the command.
 * The server plants task._cmd_interrupt, the run_command read loop consumes
 * it within ~0.2s, kills the process tree, and the partial output + the
 * interruption marker go back to the model as an ordinary tool result — the
 * turn CONTINUES. Rendered only while the round is searching (a settled
 * round has nothing to interrupt) and only when we can name the task — an
 * interrupt that cannot resolve its taskId is worse than no button. */
function _renderCmdInterruptBtn(round) {
  /* run_command AND code_exec: since pt_0bde0fd8 the standalone code_exec
   * path forwards task= into tool_run_command, so the subprocess registers
   * and the interrupt endpoint works for it identically. */
  if (!round || (round.toolName !== 'run_command' && round.toolName !== 'code_exec')) return '';
  const taskId = round._taskId || (typeof _riTaskIdForRound === 'function'
    ? _riTaskIdForRound(round) : '');
  if (!taskId) return '';
  const _tf = (typeof t === 'function') ? t : (k, d) => d;
  return `<button type="button" class="ptool-cmd-interrupt"`
    + ` data-cmd-task="${escapeHtml(String(taskId))}"`
    + ` title="${escapeHtml(_tf('toolCmd.interruptTip', 'Stop this command only — the task continues with the partial output'))}"`
    + ` onclick="_cmdInterruptClick(this,event)">${escapeHtml(_tf('toolCmd.interrupt', 'Interrupt'))}</button>`;
}

/* Click → POST the interrupt, optimistically paint "interrupting…". The row
 * settles itself when the tool_result SSE lands (the same event that would
 * have landed on a natural exit), so the success path leaves the button
 * disabled — the re-render removes it. Only a refusal (nothing to interrupt)
 * or a network failure restores it. */
async function _cmdInterruptClick(btn, ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  if (!btn || btn.disabled) return;
  const taskId = btn.getAttribute('data-cmd-task') || '';
  if (!taskId) return;
  const _tf = (typeof t === 'function') ? t : (k, d) => d;
  btn.disabled = true;
  btn.textContent = _tf('toolCmd.interrupting', 'Interrupting…');
  let r = null;
  try {
    r = (typeof Api !== 'undefined' && Api.chat)
      ? await Api.chat.interruptCommand(taskId) : null;
  } catch (_e) { r = null; }
  if (r && r.interrupted === true) return;   /* terminal frame on its way */
  btn.disabled = false;
  btn.textContent = _tf('toolCmd.interrupt', 'Interrupt');
  if (typeof showToast === 'function') {
    showToast(_tf('toolCmd.interruptNone',
      'Nothing to interrupt — the command already finished'));
  }
}

// In-flight ("searching") states: running command with live output, search
// orbit animation, or the generic active row.
function _renderSearchingRow(round, ctx) {
  const { svg, q, rootPill, cmdRootPill, repairedBadge, isSearching } = ctx;
  if (!isSearching) return "";
  // ★ run_command / code_exec: show running state with full command.
  //   If streaming output has started arriving via tool_progress events,
  //   render it live inside the block so the user can follow along.
  if (round.toolName === "run_command" || round.toolName === "code_exec") {
    const cmdRaw = round.query || "";
    const cmdText = escapeHtml(cmdRaw);
    let _cmdDesc = "";
    try {
      const _a = typeof round.toolArgs === "string" ? JSON.parse(round.toolArgs) : (round.toolArgs || {});
      _cmdDesc = (_a && _a.description) || "";
    } catch (_e) { /* malformed toolArgs — skip description */ }
    const cmdCollapsible = _cmdCollapsible(_cmdDesc, cmdRaw);
    const cmdKey = cmdCollapsible ? _cmdBodyKey(round) : '';
    const cmdOpen = cmdCollapsible && cmdKey && _cmdBodyExpanded.has(cmdKey);
    const descInlineHtml = _cmdDescInline(_cmdDesc, cmdCollapsible);
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
    /* ★ Live QR — the scan-to-login seam. A device-code / QR login command
     * PRINTS the code and then BLOCKS waiting for the scan, so this running
     * state is exactly when the user needs to scan it. The art in the live
     * <pre> above is unscannable by construction (that pane is `pre-wrap` +
     * `word-break: break-all`, which re-wraps the module rows), so the
     * backend's recovered bitmap is drawn here, before the pane. While the
     * round is in flight the descriptors live on the round itself (there is
     * no `results` entry until tool_result lands). */
    const liveQrHtml = _renderQrStrip(round);
    return `<div class="ptool-cmd-block ptool-cmd-running${cmdOpen ? ' cmd-open' : ''}"${cmdCollapsible ? ` data-cmd-key="${escapeHtml(cmdKey)}"` : ''}>
           <div class="ptool-cmd-header">
             <span class="ptool-cmd-icon">${svg}</span>
             ${cmdRootPill}
             ${descInlineHtml}
             <span class="ptool-cmd-label">Running...</span>
             ${_renderCmdTimerChip(round)}${_renderCmdInterruptBtn(round)}
             <span class="ptool-spinner"></span>
           </div>
           <pre class="ptool-cmd-code${cmdCollapsible ? ' ptool-cmd-collapsible' : ''}"><code>$ ${cmdText}</code></pre>
           ${liveQrHtml}${liveOutHtml}
         </div>`;  }
  // ★ Web search: show orbit animation
  if (_isRoundSearch(round)) {
    return `<div class="ptool-line ptool-active ptool-search-line">
           <span class="ptool-icon"><div class="search-orbit-container" style="width:16px;height:16px"><div class="search-orbit-center" style="inset:4px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div><div class="search-orbit-dot" style="width:3px;height:3px;margin:-1.5px"></div></div></span>
           <span class="ptool-text">${q}</span>${_renderBatchProgress(round)}
           <span class="ptool-spinner"></span>
         </div>`;
  }
  return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">${svg}</span>
         ${rootPill}
         <span class="ptool-text">${q}</span>
         ${repairedBadge}${_renderBatchProgress(round)}
         <span class="ptool-spinner"></span>
       </div>`;
}

/* ★ Batch per-item progress pill (pt_67ffc2b7).
 *
 * A batch call — web_search(queries=[a,b,c]) / fetch_url(urls=[…]) — is ONE
 * tool round, so before this the row showed "3 searches" + a spinner and
 * nothing else until ALL of them returned. A 2s query sitting beside a 40s one
 * was indistinguishable from three slow ones, which is precisely the "I can't
 * tell where the lag is" complaint.
 *
 * Renders nothing for a non-batch call, so the single-query path (the
 * overwhelmingly common one) is visually unchanged. */
function _renderBatchProgress(round) {
  if (!round || round._batchTotal == null) return "";
  const total = Number(round._batchTotal) || 0;
  if (total <= 1) return "";           // a 1-item "batch" adds no information
  const done = Number(round._batchDone) || 0;
  const failed = Number(round._batchFailed) || 0;
  const failHtml = failed
    ? `<span class="ptool-batch-failed" title="${escapeHtml(String(failed))} failed">${escapeHtml(String(failed))}✗</span>`
    : "";
  return `<span class="ptool-batch-progress" title="${escapeHtml(String(done))}/${escapeHtml(String(total))}">${escapeHtml(String(done))}/${escapeHtml(String(total))}${failHtml}</span>`;
}

// ★ Scannable QR recovered from terminal output (meta.qrImages, attached by
//   lib/qr.py via the shared finalize chokepoint). A QR printed as block art
//   is unscannable in the output <pre> — that pane is `white-space: pre-wrap`
//   + `word-break: break-all`, which re-wraps the module rows and destroys
//   the grid — so the backend reconstructs a real bitmap and we surface it
//   here, ABOVE the collapsed output, because the whole point is that the
//   user must be able to scan it without hunting for a toggle.
//
//   Takes any object carrying `qrImages`, because the descriptors live in two
//   places across a round's life: on the ROUND while the command is still
//   running (tool_progress delivers them before any `results` exists), and on
//   the RESULT meta once tool_result lands. Both callers pass what they have.
function _renderQrStrip(meta) {
  const qrs = Array.isArray(meta.qrImages)
    ? meta.qrImages.filter((d) => d && d.uri) : [];
  if (!qrs.length) return "";
  const tiles = qrs.map((d) => {
    const cap = escapeHtml(d.filename || "qr.png");
    return `<figure class="ptool-qr-tile">
             <img src="${escapeHtml(d.uri)}" alt="${cap}" loading="lazy"
                  onclick="event.stopPropagation();_openImageFullscreen(this.src)" />
           </figure>`;
  }).join("");
  // NOTE: t()'s second argument is a PARAMS map for {placeholder} substitution,
  // NOT a fallback string — passing prose there does nothing and the raw key
  // ("project.qrScan") is what reaches the user when the key is undefined.
  const label = qrs.length > 1
    ? `${qrs.length} ${escapeHtml(t("project.qrScanMulti"))}`
    : escapeHtml(t("project.qrScan"));
  return `<div class="ptool-qr-strip">
           <div class="ptool-qr-label">${label}</div>
           <div class="ptool-qr-grid">${tiles}</div>
         </div>`;
}

/* ── Collapsible command body (done + running states) ────────────────────
 * The one-line DESCRIPTION is what users read; the exact shell string is
 * reference detail. When a description exists AND the command is long enough
 * to be visual noise (multi-line or > 100 chars), the `$ command` <pre>
 * starts COLLAPSED and the description itself becomes the toggle (expands
 * in place — no chevron glyph, owner call 2026-08-02). Short one-liners
 * (npm test) and description-
 * less commands stay visible: collapsing the first saves nothing, collapsing
 * the second would anonymize the card. Expansion state lives in
 * _cmdBodyExpanded keyed by toolCallId, so a mid-run expand survives the
 * per-progress re-renders and a done card survives a timeline sync. */
const _cmdBodyExpanded = new Set();

function _cmdBodyKey(round) {
  return (round && round.toolCallId) ? String(round.toolCallId) : '';
}

function _cmdCollapsible(desc, cmdRaw) {
  if (!desc || !cmdRaw) return false;
  return cmdRaw.length > 100 || cmdRaw.indexOf('\n') !== -1;
}

function _cmdDescInline(desc, collapsible) {
  if (!desc) return '';
  const esc = escapeHtml(desc);
  if (!collapsible) {
    return `<span class="ptool-cmd-desc-inline" title="${esc}">${esc}</span>`;
  }
  return `<span class="ptool-cmd-desc-inline ptool-cmd-desc-toggle" title="${esc}" onclick="_cmdBodyToggle(this,event)">${esc}</span>`;
}

function _cmdBodyToggle(el, ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  const block = el && el.closest ? el.closest('.ptool-cmd-block') : null;
  if (!block) return;
  const open = !block.classList.contains('cmd-open');
  block.classList.toggle('cmd-open', open);
  const key = block.getAttribute('data-cmd-key') || '';
  if (key) {
    if (open) _cmdBodyExpanded.add(key);
    else _cmdBodyExpanded.delete(key);
  }
}

// ★ run_command / code_exec: render as inline terminal block with collapsible output
function _renderCmdDoneBlock(round, ctx) {
  const { svg, meta, cmdRootPill } = ctx;
  if (!((round.toolName === "run_command" || round.toolName === "code_exec") && (meta.command != null || meta.output != null))) return "";
  const cmdRaw = meta.command || round.query || "";
  const cmd = escapeHtml(cmdRaw);
  const cmdCollapsible = _cmdCollapsible(meta.description, cmdRaw);
  const cmdKey = cmdCollapsible ? _cmdBodyKey(round) : '';
  const cmdOpen = cmdCollapsible && cmdKey && _cmdBodyExpanded.has(cmdKey);
  const descInlineHtml = _cmdDescInline(meta.description, cmdCollapsible);
  const output = meta.output || "";
  const exitCode = meta.exitCode ?? "?";
  const timedOut = meta.timedOut || false;
  // ★ "not run": the command was refused/blocked BEFORE it executed (read-only
  //   root, dangerous pattern, no project, pre-hook block, abort, start error).
  //   There is no real exit code — show the cause, never the cryptic "exit ?".
  const notRun = meta.notRun === true || exitCode === "not-run";
  const isOk = !notRun && (exitCode === "0" || exitCode === 0);
  /* ★ Per-command interrupt (pt_232244fb): the command was killed by the
   * user button / stall watchdog but the task CONTINUED — an amber neutral
   * stop (like not-run), never the red "✗ exit -1" error frame. */
  const interrupted = meta.interrupted === true;
  const statusCls = notRun
    ? "ptool-cmd-notrun"
    : interrupted
      ? "ptool-cmd-interrupted"
      : timedOut
        ? "ptool-cmd-timeout"
        : isOk
          ? "ptool-cmd-ok"
          : "ptool-cmd-err";
  const notRunBadge = meta.badge && meta.badge !== `exit ${exitCode}`
    ? meta.badge : "not run";
  const _tfCmd = (typeof t === 'function') ? t : (k, d) => d;
  const statusLabel = notRun
    ? `⊘ ${escapeHtml(notRunBadge)}`
    : interrupted
      ? escapeHtml(_tfCmd('toolCmd.interruptedBadge', '⏸ interrupted'))
      : timedOut
        ? "timeout"
        : isOk
          ? "✓ done"
          : `✗ exit ${exitCode}`;
  // For a not-run command the reason IS the message — surface it inline
  // (not hidden behind a collapse toggle) so the user sees why immediately.
  const reason = notRun ? (meta.reason || output || "") : "";
  const qrStripHtml = _renderQrStrip(meta);
  let outputHtml = "";
  if (notRun && reason) {
    outputHtml = `<div class="ptool-cmd-reason">${escapeHtml(reason)}</div>`;
  } else if (output) {
    outputHtml = `<div class="ptool-cmd-output-wrap">
           <div class="ptool-cmd-toggle" onclick="event.stopPropagation();var w=this.parentElement;w.classList.toggle('expanded');this.textContent=w.classList.contains('expanded')?'▾ Collapse':'▸ Show output';">▸ Show output</div>
           <pre class="ptool-cmd-output"><code>${escapeHtml(output)}</code></pre>
         </div>`;
  }
  return `<div class="ptool-cmd-block ${statusCls}${cmdOpen ? ' cmd-open' : ''}" data-rn="${round.roundNum}"${cmdCollapsible ? ` data-cmd-key="${escapeHtml(cmdKey)}"` : ''}>
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${svg}</span>
           ${cmdRootPill}
           ${descInlineHtml}
           <span class="ptool-cmd-status">${statusLabel}</span>
           ${_rowRightControls(round)}
         </div>
         <pre class="ptool-cmd-code${cmdCollapsible ? ' ptool-cmd-collapsible' : ''}"><code>$ ${cmd}</code></pre>
         ${qrStripHtml}${outputHtml}
       </div>`;
}

// ★ browser_execute_js — render as an inline code block (JS in, result out),
//   mirroring the run_command terminal block. A cramped one-line "12165686:
//   (() => {…" row is unreadable; show the full snippet + collapsible result.
function _renderBrowserExecJsBlock(round, ctx) {
  const { svg, meta, rootPill } = ctx;
  if (!(round.toolName === "browser_execute_js")) return "";
  let jsCode = "";
  let jsDesc = "";
  try {
    const a = typeof round.toolArgs === "string" ? JSON.parse(round.toolArgs) : (round.toolArgs || {});
    jsCode = (a && a.code) || "";
    jsDesc = (a && a.description) || "";
  } catch (_e) { /* malformed toolArgs */ }
  const isErr = meta.badge === "error";
  const statusLabel = isErr ? "✗ error" : "✓ ok";
  const statusCls = isErr ? "ptool-cmd-err" : "ptool-cmd-ok";
  // The result returned to the model lives in round.toolContent.
  const out = typeof round.toolContent === "string" ? round.toolContent : "";
  let outputHtml = "";
  if (out) {
    outputHtml = `<div class="ptool-cmd-output-wrap">
           <div class="ptool-cmd-toggle" onclick="event.stopPropagation();var w=this.parentElement;w.classList.toggle('expanded');this.textContent=w.classList.contains('expanded')?'▾ Collapse':'▸ Show result';">▸ Show result</div>
           <pre class="ptool-cmd-output"><code>${escapeHtml(out)}</code></pre>
         </div>`;
  }
  const descHtml = jsDesc
    ? `<div class="ptool-cmd-desc">${escapeHtml(jsDesc)}</div>`
    : "";
  const codeHtml = jsCode
    ? `<pre class="ptool-cmd-code"><code>${escapeHtml(jsCode)}</code></pre>`
    : "";
  return `<div class="ptool-cmd-block ptool-cmd-js ${statusCls}" data-rn="${round.roundNum}">
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${svg}</span>
           ${rootPill}
           <span class="ptool-cmd-label">${escapeHtml(round.query || "Execute JS")}</span>
           <span class="ptool-cmd-status">${statusLabel}</span>
           ${_rowRightControls(round)}
         </div>
         ${descHtml}
         ${codeHtml}
         ${outputHtml}
       </div>`;
}

// ★ Web search / fetch — descriptive 0-result reason row, or collapsible
//   result list (per-query grouping, vertical cards, engine breakdown).
function _renderSearchRows(round, ctx) {
  const { svg, q, results } = ctx;
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
         ${_rowRightControls(round)}
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
      const _safeRu = /^https?:\/\//i.test(String(r.url || "")) ? r.url : "";
      return `<div class="search-result-item"><div class="search-result-title">${_safeRu ? `<a href="${escapeHtml(_safeRu)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a>` : `<span>${escapeHtml(r.title)}</span>`}<span class="search-result-source">${escapeHtml(r.source)}</span>${fb}</div>${r.snippet ? `<div class="search-result-snippet">${escapeHtml(r.snippet)}</div>` : ""}${r.url ? `<div class="search-result-url">${escapeHtml(r.url)}</div>` : ""}</div>`;
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
          ? `<div class="search-query-group-header"><span class="search-query-group-icon">${Icon('search', 13)}</span><span class="search-query-group-q">${escapeHtml(key)}</span><span class="search-query-group-count">${group.length}</span></div>`
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
          const urlItems = urls.map((u) => {
            const _safeEu = /^https?:\/\//i.test(String(u.url || "")) ? u.url : "";
            return `<div class="eb-url-item">${_safeEu ? `<a href="${escapeHtml(_safeEu)}" target="_blank" rel="noopener">${escapeHtml(u.title || u.url)}</a>` : `<span>${escapeHtml(u.title || u.url)}</span>`}<div class="eb-url-text">${escapeHtml(u.url)}</div></div>`;
          }).join("");
          return `<div class="eb-engine"><div class="eb-engine-name">${escapeHtml(eng)} <span class="eb-engine-count">(${urls.length})</span></div><div class="eb-engine-urls">${urlItems}</div></div>`;
        }).join("");
        engineBkdnHtml = `<div class="eb-section">
          <div class="eb-toggle" onclick="event.stopPropagation();this.parentElement.classList.toggle('eb-expanded')">🔍 Engine Sources <span class="eb-total">${totalRaw} raw → ${results.length} final</span> <span class="eb-arrow">▸</span></div>
          <div class="eb-content">${ebInner}</div>
        </div>`;
      }
    }
    return `<div class="ptool-results-block" data-rn="${round.roundNum}">
         <div class="ptool-line ptool-results-header" onclick="if(event.target.closest('.ri-tool-anchor'))return;event.stopPropagation();this.parentElement.classList.toggle('expanded')">
           <span class="ptool-icon">${svg}</span>
           <span class="ptool-text">${q}</span>
           ${verts.length ? (()=>{const doms=[...new Set(verts.map(v=>v.domain||'').filter(Boolean))];return `<span class="ptool-badge vertical-badge" title="Vertical domain data">vertical: ${escapeHtml(doms.join(' · ') || 'auto')}</span>`;})() : ''}
           <span class="ptool-badge ptool-badge-info">${results.length} result${results.length !== 1 ? "s" : ""}</span>
           ${_rowRightControls(round)}
           <span class="ptool-results-toggle">▼</span>
         </div>
         <div class="ptool-results-content">${verticalHtml}${items}${engineBkdnHtml}</div>
       </div>`;
  }
  return "";
}

// ★ read_files / inspect_image / browser screenshot+preview image(s): render
//   inline thumbnails when the backend attached data URIs (meta.imageDataUris).
//   Each descriptor carries a full data: URL the browser can render directly.
//   inspect_image is the zoom/rotate/crop viewer — it gets a distinct accent +
//   an "ops" chip describing the transform (e.g. "crop, 2×").
function _renderReadImagesBlock(round, ctx) {
  const { svg, q, meta } = ctx;
  if (!((round.toolName === "read_files" || round.toolName === "inspect_image" ||
        round.toolName === "browser_screenshot" || round.toolName === "browser_preview_page") &&
      Array.isArray(meta.imageDataUris) && meta.imageDataUris.length)) return "";
  const imgs = meta.imageDataUris.filter((d) => d && d.uri);
  if (!imgs.length) return "";
  const isInspect = round.toolName === "inspect_image";
  const multi = imgs.length > 1;
  const tiles = imgs.map((d) => {
    const cap = escapeHtml(d.filename || d.format || "");
    return `<figure class="rf-img-tile">
             <img src="${escapeHtml(d.uri)}" alt="${cap}" loading="lazy"
                  onclick="_openImageFullscreen(this.src)" />
             ${cap ? `<figcaption class="rf-img-cap" title="${cap}">${cap}</figcaption>` : ""}
           </figure>`;
  }).join("");
  // inspect_image: prefer the ops badge (crop/zoom/rotate); read_files
  // multi: image count; else fall back to the generic meta badge.
  const opsChip = isInspect && meta.inspectOps
    ? `<span class="ptool-badge rf-inspect-chip" title="${escapeHtml(_localizeInspectOps(t, meta.inspectOps, "title"))}">${escapeHtml(_localizeInspectOps(t, meta.inspectOps))}</span>`
    : "";
  const countBadge = multi
    ? `<span class="ptool-badge ptool-badge-info">${imgs.length} images</span>`
    : (!isInspect && meta.badge ? `<span class="ptool-badge ptool-badge-info">${escapeHtml(meta.badge)}</span>` : "");
  return `<div class="ptool-readimg-block${isInspect ? " ptool-inspectimg-block" : ""}" data-rn="${round.roundNum}">
           <div class="ptool-line ptool-readimg-header">
             <span class="ptool-icon">${svg}</span>
             <span class="ptool-text">${q}</span>
             ${opsChip}
             ${countBadge}
             ${_rowRightControls(round)}
           </div>
           <div class="rf-img-grid${multi ? " rf-img-grid-multi" : ""}${isInspect ? " rf-img-grid-inspect" : ""}">${tiles}</div>
         </div>`;
}

// ★ Image generation / editing: render inline image card.
//   The two functions are visually distinguished by a mode theme:
//   • generate → magenta "Generated" theme + framed-photo icon
//   • edit     → cyan "Edited" theme + wand icon + before→after strip
function _renderImageGenBlock(round, ctx) {
  const { svg, q, meta } = ctx;
  if (!_isRoundImageGen(round)) return "";
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
      projPath ? `<span class="ig-path-chip" title="Saved to project: ${escapeHtml(projPath)}"><span class="ig-path-icon">${Icon('image', 12)}</span>${escapeHtml(projPath)}</span>` : "",
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
             ${_rowRightControls(round)}
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
             ${_rowRightControls(round)}
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

/* ── Write-gate refusal presentation ─────────────────────────────────
 * The shared-worktree guards (read-before-edit + write-freshness,
 * lib/tasks_pkg/handlers/project.py) REFUSE a write tool call: nothing
 * executed, the model re-reads and re-issues. The raw badge tokens
 * ('stale', 'read first', 'partial: …', 'ref failed') are developer
 * jargon — meaningless on a user-facing card. New rounds carry
 * structured meta.refusal {kind, paths, skipped, proceeded}; older
 * persisted rounds only have the badge string, still recognized below.
 * Rendering: a localized amber badge (an interception, not a crash)
 * with the reason as tooltip, plus an explanation card naming the
 * file(s) and the automatic next step. */
const _GATE_REFUSAL_BADGE_KINDS = {
  "stale": "stale",
  "read first": "read_first",
  "partial: stale": "partial_stale",
  "partial: read first": "partial_read_first",
  "ref failed": "content_ref",
};
const _GATE_REFUSAL_TOOLS = ["write_file", "apply_diff", "apply_diffs", "insert_content", "insert_contents"];

function _refusalInfo(round, meta) {
  if (!round || !meta || _GATE_REFUSAL_TOOLS.indexOf(round.toolName) === -1) return null;
  const r = meta.refusal;
  if (r && typeof r === "object" && typeof r.kind === "string" && r.kind) {
    return {
      kind: r.kind,
      paths: Array.isArray(r.paths) ? r.paths.filter(function (p) { return !!p; }) : [],
      skipped: r.skipped | 0,
      proceeded: r.proceeded | 0,
    };
  }
  const kind = _GATE_REFUSAL_BADGE_KINDS[meta.badge];
  return kind ? { kind: kind, paths: [], skipped: 0, proceeded: 0 } : null;
}

function _gateRefusalBadgeLabel(kind) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const M = {
    stale: ["tool.gateStaleBadge", "changed on disk"],
    read_first: ["tool.gateReadFirstBadge", "must read first"],
    partial_stale: ["tool.gatePartialStaleBadge", "partial · changed"],
    partial_read_first: ["tool.gatePartialReadFirstBadge", "partial · unread"],
    content_ref: ["tool.gateContentRefBadge", "content ref failed"],
  };
  const e = M[kind];
  return e ? _t(e[0], e[1]) : kind;
}

function _gateRefusalTitle(kind, info) {
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const M = {
    stale: ["tool.gateStaleTitle", "Write blocked — file changed on disk"],
    read_first: ["tool.gateReadFirstTitle", "Edit blocked — file not read in this conversation yet"],
    partial_stale: ["tool.gatePartialStaleTitle", "{skipped} edit(s) blocked — target file(s) changed on disk"],
    partial_read_first: ["tool.gatePartialReadFirstTitle", "{skipped} edit(s) blocked — must read first"],
    content_ref: ["tool.gateContentRefTitle", "Write not executed — content reference failed"],
  };
  const e = M[kind];
  if (!e) return "";
  return _t(e[0], e[1]).split("{skipped}").join(String((info && info.skipped) || 0));
}

function _renderGateNotice(info) {
  if (!info) return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const TEXT = {
    stale: ["tool.gateStaleText",
      "{paths} was modified by another conversation or process after this conversation last read/wrote it. To avoid silently overwriting their change, this write was NOT executed — the assistant will re-read the file and re-issue the edit; no action needed from you."],
    read_first: ["tool.gateReadFirstText",
      "The read-before-edit guard requires reading {paths} with read_files in this conversation before patching it, so patches are never built from guessed or remembered content. This edit was NOT executed — the assistant will read the file first and re-issue."],
    partial_stale: ["tool.gatePartialStaleText",
      "{paths} changed on disk after this conversation last read/wrote it, so {skipped} edit(s) targeting it were NOT executed; the other {proceeded} edit(s) ran normally. The blocked edits will be re-issued after a fresh read."],
    partial_read_first: ["tool.gatePartialReadFirstText",
      "{paths} has not been read in this conversation, so {skipped} edit(s) targeting it were NOT executed; the other {proceeded} edit(s) ran normally. The assistant will read the file and re-issue the blocked edits."],
    content_ref: ["tool.gateContentRefText",
      "The content_ref used by write_file points to a previous tool result that does not exist or has no content. The assistant will retry with explicit content instead."],
  };
  const e = TEXT[info.kind];
  const title = _gateRefusalTitle(info.kind, info);
  if (!e || !title) return "";
  const pathsHtml = (info.paths || []).map(function (p) {
    const base = p.split("/").filter(Boolean).pop() || p;
    return `<code class="ptool-gate-note-path" title="${escapeHtml(p)}">${escapeHtml(base)}</code>`;
  }).join(", ");
  const targetFallback = escapeHtml(_t("tool.gateTargetGeneric", "The target file"));
  const raw = _t(e[0], e[1])
    .split("{paths}").join("\x00P\x00")
    .split("{skipped}").join(String(info.skipped || 0))
    .split("{proceeded}").join(String(info.proceeded || 0));
  const textHtml = escapeHtml(raw).split("\x00P\x00").join(pathsHtml || targetFallback);
  const shieldSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
  /* The leading \n lives INSIDE the return value so a "" (non-refusal)
   * splice at the call site is byte-identical to not having the call at
   * all — the tool-rounds wire-parity baseline freezes non-refusal markup. */
  return `\n             <div class="ptool-gate-note">
      <span class="ptool-gate-note-icon icon-box">${shieldSvg}</span>
      <div class="ptool-gate-note-body">
        <div class="ptool-gate-note-title">${escapeHtml(title)}</div>
        <div class="ptool-gate-note-text">${textHtml}</div>
      </div>
    </div>`;
}

// Determine the trailing badge (explicit meta.badge → token count → fetched
// chars → generic ✓ done).
function _computeToolBadgeHtml(round, ctx) {
  const { meta, results } = ctx;
  let badgeHtml = "";
  if (meta.badge) {
    const refusal = _refusalInfo(round, meta);
    if (refusal) {
      const tip = _gateRefusalTitle(refusal.kind, refusal);
      badgeHtml = `<span class="ptool-badge ptool-badge-warn ptool-badge-gate"${tip ? ` title="${escapeHtml(tip)}"` : ""}>${escapeHtml(_gateRefusalBadgeLabel(refusal.kind))}</span>`;
    } else {
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
    }
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
  return badgeHtml;
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
function _renderCompactionLabel(round) {
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
  return compactionLabelHtml;
}

// ★ write_file — collapsible inline preview of the written content,
//   mirroring the apply_diff expand-on-click block. The full content
//   lives in round.toolArgs.content; render it as added lines so the
//   user can review what was written instead of an opaque "Preview".
function _renderWriteFileBlock(round, ctx, badgeHtml, compactionLabelHtml) {
  const { svg, q, rootPill, meta } = ctx;
  if (!(round.toolName === "write_file" && round.toolArgs)) return "";
  let pe = null;
  try { pe = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs; } catch (_) {}
  if (!(pe && typeof pe.content === 'string' && pe.content.length)) return "";
  const diffHtml = _renderLineDiff("", pe.content);
  if (!diffHtml) return "";
  const gateNoticeHtml = _renderGateNotice(_refusalInfo(round, meta));
  return `<details class="ptool-batch-done-block" data-rn="${round.roundNum}">
             <summary class="ptool-line ptool-batch-done-header">
               <span class="ptool-icon">${svg}</span>
               ${compactionLabelHtml}
               ${rootPill}
               <span class="ptool-text">${q}</span>
               ${badgeHtml}
             </summary>${gateNoticeHtml}
             <div class="ptool-batch-done-list">
               <div class="ptool-batch-done-single">${diffHtml}</div>
             </div>
           </details>`;
}

// ★ Single apply_diff / insert_content — collapsible inline diff
function _renderSingleDiffBlock(round, ctx, badgeHtml, compactionLabelHtml) {
  const { svg, q, meta, rootPill } = ctx;
  if (!(!meta.editSummaries && (round.toolName === "apply_diff" || round.toolName === "insert_content") && round.toolArgs)) return "";
  let pe = null;
  try { pe = typeof round.toolArgs === 'string' ? JSON.parse(round.toolArgs) : round.toolArgs; } catch (_) {}
  if (!(pe && (pe.search || pe.anchor))) return "";
  const isInsert = !pe.search && pe.anchor;
  const oldText = pe.search || pe.anchor || "";
  const newText = isInsert
    ? ((pe.position === "before" ? (pe.content + "\n") : "") + (pe.anchor || "") + (pe.position !== "before" ? ("\n" + pe.content) : ""))
    : (pe.replace || "");
  const diffHtml = _renderLineDiff(oldText, newText);
  if (!diffHtml) return "";
  const gateNoticeHtml = _renderGateNotice(_refusalInfo(round, meta));
  return `<details class="ptool-batch-done-block" data-rn="${round.roundNum}">
             <summary class="ptool-line ptool-batch-done-header">
               <span class="ptool-icon">${svg}</span>
               ${compactionLabelHtml}
               ${rootPill}
               <span class="ptool-text">${q}</span>
               ${badgeHtml}
             </summary>${gateNoticeHtml}
             <div class="ptool-batch-done-list">
               <div class="ptool-batch-done-single">${diffHtml}</div>
             </div>
           </details>`;
}

// ★ Batch edit tools (apply_diffs / insert_contents) — collapsible per-edit list
//   Guard on toolName: editSummaries is only meaningful for the batch edit
//   tools. Without this guard, ANY round whose results[0] happens to carry
//   an editSummaries array (e.g. a tool_result leaked from a sub-agent's
//   apply_diffs grafted onto a same-roundNum run_command — see
//   sse_handlers_tool.js roundNum fallback) renders as a batch-edit block.
function _renderBatchEditsBlock(round, ctx, badgeHtml, compactionLabelHtml) {
  const { svg, q, meta, rootPill } = ctx;
  const _isBatchEditTool = round.toolName === "apply_diffs" || round.toolName === "insert_contents";
  if (!(_isBatchEditTool && meta.editSummaries && Array.isArray(meta.editSummaries) && meta.editSummaries.length > 1)) return "";
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
  const gateNoticeHtml = _renderGateNotice(_refusalInfo(round, meta));
  return `<details class="ptool-batch-done-block" open data-rn="${round.roundNum}">
         <summary class="ptool-line ptool-batch-done-header">
           <span class="ptool-icon">${svg}</span>
           ${compactionLabelHtml}
           ${rootPill}
           <span class="ptool-text">${q}</span>
           ${badgeHtml}
         </summary>${gateNoticeHtml}
         <div class="ptool-batch-done-list">${itemsHtml}</div>
       </details>`;
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
        pollId: p.poll_id || p.pollId || '',
        decision: p.decision || 'wait',
        reason: (p.reason || '').slice(0, 200),
        rawContent: p.raw_output || p.rawContent || '',
        tokensUsed: p.tokens_used || 0,
        timerId: timerId,
        model: p.model || '',
        cmdOutput: p.check_output || '',
        parseError: p.decision === 'parse_error',
        toolTrace: [],  // not persisted per-poll; live trace only
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
        if (typeof twUpdate === 'function') twUpdate(activeConvId);
      }
      console.info(`[Timer] Recovered ${recoveredPolls.length} polls for timer ${timerId.slice(0,12)}`);
    }
  } catch (e) {
    console.debug('[Timer] Poll recovery failed:', e.message);
  }
}

/* ── Parallel-batch grouping ──────────────────────────────────────────
 * A single LLM turn (one assistant message) can carry several tool_calls
 * that the harness runs together. The backend tags every such round with
 * the SAME `llmRound` (= orchestrator loop index, see
 * lib/tasks_pkg/tool_dispatch.py). Rounds with the same llmRound were
 * therefore issued IN PARALLEL; rounds with different llmRound are
 * sequential turns. We group contiguous same-llmRound rounds into one
 * `.ptool-turn` container so the UI reflects the real parallelism instead
 * of a flat list.
 *
 * Accuracy guard: we ONLY group on real `llmRound` data. Legacy rounds
 * without it are each their own (solo) group — the old roundNum-gap
 * heuristic is too unreliable to *claim* parallelism visually. */
function _computeToolBatches(rounds) {
  const hasLlm = (rounds || []).some((r) => r && r.llmRound != null);
  const groups = [];
  let cur = null;
  for (const r of rounds || []) {
    const key = hasLlm && r.llmRound != null ? "L" + r.llmRound : "S" + r.roundNum;
    if (!cur || cur.key !== key) { cur = { key, rounds: [] }; groups.push(cur); }
    cur.rounds.push(r);
  }
  return groups;
}

/* Count distinct LLM turns represented by a set of rounds (for the panel
 * header). Falls back to round count when no llmRound data is present. */
function _countToolTurns(rounds) {
  const set = new Set();
  let hasLlm = false;
  for (const r of rounds || []) {
    if (r && r.llmRound != null) { hasLlm = true; set.add(r.llmRound); }
  }
  return hasLlm ? set.size : (rounds || []).length;
}

/* Panel header text — "N tools used" plus a "· M turns" suffix only when
 * parallelism actually compressed the turn count (M < N). */
function _toolPanelHeaderLabel(rounds, anyActive) {
  const count = (rounds || []).length;
  if (anyActive) return t("toolPanel.working", { n: count });
  const turns = _countToolTurns(rounds);
  const base = t("toolPanel.toolsUsed", { n: count, s: count !== 1 ? "s" : "" });
  return turns < count
    ? base + t("toolPanel.turnsSuffix", { n: turns, s: turns !== 1 ? "s" : "" })
    : base;
}

/* git-fork glyph (two parents → one child) — reads as "these calls
 * branched off the same turn". SVG only, per CLAUDE.md §3.4. */
const _turnForkSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></svg>';

function _turnLabelText(size) {
  return t("toolPanel.parallelCalls", { n: size });
}

/* Round number a tool-batch maps to. A group's llmRound is 0-based; the
 * cost popover labels the matching API round 1-based as `第N轮`, so
 * N = llmRound + 1. Returns null for legacy rounds with no llmRound
 * (we never claim a round number we don't actually have). */
function _groupRoundNo(g) {
  const r0 = g && g.rounds && g.rounds[0];
  return r0 && r0.llmRound != null ? r0.llmRound + 1 : null;
}

/* Human label for a round number — uses the shared `toolPanel.roundTag`
 * i18n key (also used by the cost popover in finish_info.js) so the two
 * panels line up 1:1 and a user can trace one round across both. */
function _roundTagText(rno) {
  return t("toolPanel.roundTag", { n: rno });
}

/* Standalone round tag for a SOLO turn (one tool call). Solo groups are
 * display:contents (no box), so this renders as a thin label line above
 * the single tool row. */
function _renderSoloRoundTag(rno) {
  return `<div class="ptool-turn-rno ptool-turn-rno-solo" title="${escapeHtml(_roundTagText(rno))}">${escapeHtml(String(rno))}</div>`;
}

/* The collapsible header shown atop a multi-call .ptool-turn container.
 * Shared verbatim by the static renderer and the streaming sync path so
 * a finalize swap is seamless. `rno` (optional) prefixes the round number
 * so the header reads e.g. `第3轮 · 2 parallel calls`. */
function _renderTurnHead(size, rno) {
  const rnoHtml = rno != null ? `<span class="ptool-turn-rno" title="${escapeHtml(_roundTagText(rno))}">${escapeHtml(String(rno))}</span>` : "";
  return `<div class="ptool-turn-head">${rnoHtml}${_turnForkSvg}<span class="ptool-turn-label">${_turnLabelText(size)}</span><span class="ptool-turn-chev">▾</span></div>`;
}

/* Render one tool round into its `[data-prn]` slot. Swarm rounds get the
 * full agent dashboard; everything else the compact tool line. `allRounds`
 * is the full timeline (swarm panels need it for cross-round context).
 *
 * The debug entry rides inside the row's own header (see _rowRightControls),
 * occupying its own space in the row's flex flow instead of floating over
 * anything. Swarm panels have no `.ptool-line` header of that shape, so they
 * — and only they — still get a standalone entry appended after the panel. */
function _renderToolSlot(r, allRounds) {
  const isSwarm = _isRoundSwarm(r);
  const inner = (isSwarm && typeof _buildSwarmPanelHTML === 'function')
    ? _buildSwarmPanelHTML(r, allRounds)
    : _renderUnifiedToolLine(r, r.status === "searching");  // panel DEFERRED: generic line (Epic-E sub-5B)
  const swarmAttr = isSwarm ? ' data-prn-kind="swarm"' : '';
  const trailing = isSwarm ? _renderStandaloneDebugEntry(r) : '';
  return `<div data-prn="${r.roundNum}"${swarmAttr}>${inner}${trailing}</div>`;
}

/* ── Debug entry per TOOL ROW ────────────────────────────────────────────
 * The owner's original complaint: "I see a suspicious tool call in chatinner
 * and there is no way to find WHICH request produced it." A single per-bubble
 * anchor is the wrong granularity — one bubble holds N rounds x M tool calls.
 * So every tool row carries its own entry.
 *
 * The mapping needs no new backend data: the backend tags each round with
 * `llmRound` (0-based orchestrator loop index, see _computeToolBatches), and
 * the request snapshot's `roundNum` is 1-based — so this row was PRODUCED by
 * request R(llmRound+1), and its result was carried INTO R(llmRound+2).
 *
 * ONE entry, ONE view. The former R (producing request) and S (post-tool state
 * mirror) buttons were two controls onto the same round; they briefly became
 * two tabs inside one panel, and on 2026-07-29 the owner removed the second
 * one outright ("we don't need both a request and a result status button") —
 * correctly, because the mirror is captured AFTER the tool results are appended
 * to the same message list the request was built from, so it is a SUPERSET of
 * the request. The request axis survives only as a fallback for rounds that
 * emitted no mirror. (The separate verbatim "model view" button was removed on
 * 2026-07-28 per owner directive — the round-scoped record this entry shows is
 * the surviving way to inspect what a tool call saw and returned.)
 *
 * `data-ri-state` addresses this row's state mirror so the drawer's state list
 * can find this slot for its inline jump.
 *
 * Rendered ONLY in debug mode, and only when we can name a task + round. */
function _renderDebugEntry(r) {
  if (typeof _featureFlags === 'undefined' || !_featureFlags.debug_mode) return '';
  if (!r || r._inboxInject || r._peerInject || r._userSteerInject || r._stallNudge) return '';
  const taskId = r._taskId || (typeof _riTaskIdForRound === 'function'
    ? _riTaskIdForRound(r) : '');
  const lr = r.llmRound;
  if (!taskId || lr == null) return '';
  const round = Number(lr) + 1;          // llmRound 0-based → roundNum 1-based
  const tip = (typeof t === 'function') ? t('ri.toolAnchorTip', { round }) : '';
  const esc = escapeHtml(String(taskId));
  return `<button type="button" class="ri-tool-anchor" ` +
    `data-ri-state="${esc}:${round}" ` +
    `title="${escapeHtml(tip)}" ` +
    `onclick="openToolDebugPanel('${esc}',${round},this)">` +
    `${_RI_TOOL_ANCHOR_SVG}<span class="ri-tool-anchor-label">R${round}</span></button>`;
}

/* Swarm-panel variant: the dashboard has no shared header to sit in, so the
 * entry gets its own right-aligned strip UNDER the panel. Still a real block
 * in normal flow — never a negative-margin overlay. */
function _renderStandaloneDebugEntry(r) {
  const btn = _renderDebugEntry(r);
  return btn ? `<div class="ri-tool-anchor-row">${btn}</div>` : '';
}

/* The row's right-hand control group — the SINGLE owner of a tool row's right
 * edge. Every control lives here and each occupies its own space, so nothing
 * can overlap: previously `.tc-preview-btn` claimed the right end with
 * `margin-left:auto` while the debug entry floated over it from a zero-height
 * block. Now `margin-left:auto` belongs to this wrapper alone.
 *
 * The debug entry is the ONLY control left: the verbatim "model view" button
 * was removed on 2026-07-28 per owner directive (its content duplicated what
 * the debug panel answers better, round-scoped). */
function _rowRightControls(round) {
  return `<span class="ptool-row-ctl">${_renderDebugEntry(round)}</span>`;
}

/* Code-glyph SVG (§3.4: SVG only, never a unicode glyph as a control). */
const _RI_TOOL_ANCHOR_SVG =
  '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="m8 6-6 6 6 6"/><path d="m16 6 6 6-6 6"/></svg>';

/* Build a llmRound-key → [narration text segments] map from a message's
 * `segments`. Mirrors the timeline's narration selection EXACTLY (non-
 * deliverable `text` segments only; thinking stays the grouped bottom block).
 * Keyed "L<llmRound>" to match _computeToolBatches' batch key so the grouped
 * panel can render each round's narration adjacent to that round's tools. */
function _narrationByRound(segments) {
  const m = new Map();
  if (!Array.isArray(segments)) return m;
  for (const s of segments) {
    if (!s || s.type !== "text" || s.deliverable) continue;
    if (s.llmRound == null) continue;
    const en = s.text || "";
    const zh = s.translatedText || "";
    if (!en.trim() && !zh.trim()) continue;
    const key = "L" + s.llmRound;
    if (!m.has(key)) m.set(key, []);
    m.get(key).push(s);
  }
  return m;
}

/* Render a round's narration segments as flat `.md-content.seg-narration`
 * blocks — BYTE-IDENTICAL to the settled timeline (_renderTimelineBatch) and
 * to the live streaming preview (translation_render.js paints the SAME class
 * list since Phase 3.5 step 2; formerly `.stream-seg-narration`): show the
 * per-round Chinese (seg.translatedText, stamped by the incremental translator)
 * when present, else the English narration. This is what makes per-round
 * translation render IN PLACE in the grouped panel (toggle OFF / timeline
 * fallback) instead of clumping into one block at the tail. */
function _renderSegNarrationHTML(segs) {
  let html = "";
  for (const s of (segs || [])) {
    const _segText = (s.translatedText && s.translatedText.trim()) ? s.translatedText : s.text;
    if (!_segText || !_segText.trim()) continue;
    const _segClean = (typeof stripNoTranslateTags === "function") ? stripNoTranslateTags(_segText) : _segText;
    /* data-seg-round keys this narration block to its llmRound so the unified
     * per-round translate painter (_applyPartialByRoundToSettled) can update
     * just this block's Chinese in place when a retro/on-open translation
     * streams round-by-round — no whole-bubble swap. Mirrors the live preview's
     * `.seg-narration[data-seg-round]` (same settled class since Phase 3.5
     * step 2). */
    const _rk = (s.llmRound != null) ? ` data-seg-round="L${escapeHtml(String(s.llmRound))}"` : '';
    html += `<div class="md-content seg-narration"${_rk}>${renderMarkdown(_segClean)}</div>`;
  }
  return html;
}

/* Render the full grouped inner HTML of the panel body: one `.ptool-turn`
 * per batch. Solo turns get no chrome (CSS collapses the wrapper via
 * display:contents); multi-call turns get the collapsible parallel header.
 * `narrByRound` (optional, from _narrationByRound) prepends each round's
 * narration prose as a flat sibling BEFORE its `.ptool-turn` — the settled
 * grouped-panel analogue of the timeline's inline narration slot.
 * Used by the static render path AND the upload.js "expand all" handler. */
function _renderToolGroupsHTML(rounds, allRounds, narrByRound) {
  const ctx = allRounds || rounds;
  return _computeToolBatches(rounds).map((g) => {
    const slots = g.rounds.map((r) => _renderToolSlot(r, ctx)).join("");
    const size = g.rounds.length;
    const rno = _groupRoundNo(g);
    const head = size >= 2 ? _renderTurnHead(size, rno) : (rno != null ? _renderSoloRoundTag(rno) : "");
    const narr = (narrByRound && narrByRound.get) ? _renderSegNarrationHTML(narrByRound.get(g.key)) : "";
    return narr + `<div class="ptool-turn" data-llm-round="${escapeHtml(String(g.key))}" data-batch-size="${size}" data-round-no="${rno != null ? rno : ""}">${head}${slots}</div>`;
  }).join("");
}

/* ═══════════════════════════════════════════════════════════════════
   INTERLEAVED SEGMENT TIMELINE (epic pt_8b406df8fbe24ae5, step 5)

   Renders a finished assistant turn from its ordered `segments` list
   (the backend SoT, docs/EPIC_SEGMENT_TIMELINE_DESIGN.md) so each tool's
   PRECEDING thinking + narration render ADJACENT to it, inline — instead
   of the three global grouped blocks (all tools / all thinking / all
   content) the legacy path produces.

   DATA CONTRACT (mirrors the backend rehydrate philosophy): segments carry
   the ORDER + the prose. Tool BODIES are NOT re-derived from the thin
   `tool_use` segment (which lacks query/results/_swarm/interactive-card
   fields) — they are looked up in the still-present, render-rich
   `msg.toolRounds` full objects by tool-call id (fallback: positional).
   So this helper needs BOTH segments (order/prose) and toolRounds (bodies);
   if either is missing the caller falls back to the legacy grouped render.

   Reuses `.ptool-panel` / `.ptool-turn` / `.thinking-block` / `.md-content`
   verbatim — NO new CSS (styles.css is under a sibling's hold; and reusing
   the proven classes keeps the flag-off/on visuals consistent).
   ═══════════════════════════════════════════════════════════════════ */

/* Build a tool-call-id → round lookup (fallback to positional order for
 * legacy rounds that predate stable toolCallId stamping). */
function _roundsByToolCallId(rounds) {
  const byId = new Map();
  const noId = [];
  for (const r of (rounds || [])) {
    if (r && r.toolCallId) byId.set(String(r.toolCallId), r);
    else noId.push(r);
  }
  return { byId, noId };
}

/* Render one interleaved batch: the batch's thinking + narration prose
 * (from segments) followed by its tool rows (from toolRounds, grouped/
 * rendered by the existing _renderToolGroupsHTML). `batch` = ordered list
 * of segments sharing one llmRound; `rounds` = the resolved full rounds
 * for that batch's tool_use segments; `allRounds` = full timeline (swarm
 * context). idx = message index (thinking-block lazy-load hook). */
function _renderTimelineBatch(batch, rounds, allRounds, idx) {
  let html = "";
  // Prose first (thinking, then narration text) — the order the model
  // produced it before it called the tools. Non-deliverable text only;
  // the deliverable answer is rendered by the caller AFTER the timeline.
  for (const s of batch) {
    if (s.type === "thinking" && s.text) {
      const len = s.text.length;
      const meta = len >= 1024 ? ` (${Math.round(len / 1024)}k chars)` : ` (${len} chars)`;
      /* Per-batch thinking: the text is segment-local (NOT msg.thinking), so
       * we can't reuse the _toggleThinking lazy-load (which reads
       * msg.thinking). Inject the escaped text directly with a pure-CSS
       * expand toggle (same idiom as the streaming .thinking-block). Per-batch
       * reasoning is bounded, so no lazy-load needed here. Reuses the
       * .thinking-block classes verbatim — no new CSS. */
      html += `<div class="thinking-block seg-thinking" onclick="this.classList.toggle('expanded')"><div class="thinking-header"><span class="thinking-label">${escapeHtml(t('stream.thinking.done'))}${meta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text">${escapeHtml(s.text)}</div></div></div>`;
    } else if (s.type === "text" && !s.deliverable && s.text) {
      /* Inter-round narration ("Let me check the files.") rendered as its
       * own quiet content block, adjacent to the tools it preceded.
       * ★ When auto-translate committed a per-round Chinese projection onto
       *   this segment (seg.translatedText, stamped by the incremental
       *   translator via _commit_translation_to_db → _stamp_segment_translations),
       *   render THAT so the settled timeline stays interleaved exactly like
       *   the streaming preview — no de-interleaved snap-back at finalize. The
       *   bilingual 原文/译文 toggle still gives English on demand. Falls back
       *   to English when the field is absent (auto-translate off / pre-v36). */
      const _segText = (s.translatedText && s.translatedText.trim()) ? s.translatedText : s.text;
      /* Strip any surviving <notranslate>/<nt> tags or ⟦NT_n⟧ placeholders
       * (incl. the mangled/localized 【NT_n】 forms cheap LLMs leave behind —
       * see lib/translate/notranslate.py) before rendering, exactly like the
       * streaming preview (translation.js) and the settled bilingual view
       * (chat_render.js) do. Without this the settled tool log was the one
       * translated-content site that leaked the raw marker — a clean→dirty
       * snap at finalize. */
      const _segClean = (typeof stripNoTranslateTags === 'function') ? stripNoTranslateTags(_segText) : _segText;
      /* data-seg-round: surgical-update key for the unified per-round translate
       * painter (see _renderSegNarrationHTML). */
      const _rk = (s.llmRound != null) ? ` data-seg-round="L${escapeHtml(String(s.llmRound))}"` : '';
      html += `<div class="md-content seg-narration"${_rk}>${renderMarkdown(_segClean)}</div>`;
    }
  }
  // Then the tool rows for this batch (rich bodies from toolRounds).
  if (rounds.length > 0) {
    html += _renderToolGroupsHTML(rounds, allRounds);
  }
  return html;
}

/* Render the interleaved per-tool timeline for a finished message.
 * Returns HTML, or "" when the segment path can't apply (caller falls
 * back to the legacy grouped render). Pure — no DOM mutation. */
function renderSegmentTimelineHTML(segments, msg, idx) {
  if (!Array.isArray(segments) || segments.length === 0) return "";
  const allRounds = getToolRoundsFromMsg(msg) || [];
  /* ── Extract synthetic inject rows (steer / peer / async swarm) ───────────
   * These display-only chips are DELIBERATELY absent from `segments` (backend
   * assemble_segments skips is_synthetic_inbox_round), so the batch walk below
   * — which is driven purely by segments — would DROP them entirely (the
   * "settled loses the chip" bug). Pull them out of `allRounds` here, key each
   * by its ANCHOR llmRound (injectRound-1, the round that consumed it, same
   * rule as _spliceInjectRow), and prepend its rendered chip before that
   * round's batch. Real-round resolution + the header count below use
   * `realRounds` ONLY, so a synthetic row (no toolCallId) can never be picked
   * up as a positional tool body nor inflate the "N tools" header. */
  const _injByAnchor = new Map();
  const realRounds = [];
  /* ★ Superseded-orphan drop, SEGMENT-TIMELINE path (parity with
   *   renderToolRoundsHTML's filter). A FloorRetry / stream-retry duplicate
   *   whose tc_id never survived into the final assistant_msg is stamped
   *   badge='superseded' (result-less) by the backend reconcile_announced_rounds
   *   — its adopted/recovered twin is the real call. This path renders from
   *   `segments` (assemble_segments mints a tool_use segment for EVERY round,
   *   carrying only {content,status} with NO badge), so the toolRounds-side
   *   filter never runs here — the exact coverage gap that left a misleading
   *   "interrupted" chip on a reloaded turn. Drop these rounds from `realRounds`
   *   EARLY so both the render AND the header count / data-full-count (which read
   *   realRounds.length) exclude them together; record their tc_ids so the
   *   segment walk skips the matching tool_use WITHOUT falling to positional
   *   no-id resolution (which could otherwise mis-grab an unrelated round).
   *   Authoritative signal is the 'superseded' badge — NOT blind name+arg
   *   structural matching (a turn may legitimately call the same tool twice). */
  const _supersededTcIds = new Set();
  for (const r of allRounds) {
    if (r && (r._userSteerInject || r._peerInject || r._inboxInject || r._stallNudge)) {
      const injRound = r._userSteerInject ? r.steerRound
        : (r._peerInject ? r.peerRound
          : (r._stallNudge ? r.stallRound : r.inboxRound));
      const anchor = (injRound || 0) - 1;
      if (!_injByAnchor.has(anchor)) _injByAnchor.set(anchor, []);
      _injByAnchor.get(anchor).push(r);
    } else if (_isSupersededOrphanRound(r)) {
      if (r && r.toolCallId) _supersededTcIds.add(String(r.toolCallId));
    } else {
      realRounds.push(r);
    }
  }
  /* END_INJECT_EXTRACTION */
  const { byId, noId } = _roundsByToolCallId(realRounds);
  let noIdCursor = 0;

  // Walk segments, accumulating consecutive segments of the same llmRound
  // into a batch. A batch flushes when the llmRound changes OR at the end.
  // Terminal (deliverable) segments are NOT part of the timeline — the
  // caller renders the deliverable answer separately, after the panel.
  const batches = [];
  let cur = null;
  // Sentinel that never equals a real batch key ("L<n>" / "S"); `cur === null`
  // on the first iteration forces a fresh batch regardless. (Was a Symbol,
  // which tsc flags as a string-vs-symbol comparison that can never match.)
  let curKey = null;
  for (const s of segments) {
    if (s.terminal) continue;               // deliverable answer / terminal thinking
    if (s.type === "text" && s.deliverable) continue;  // safety: never in timeline
    const key = (s.llmRound != null) ? ("L" + s.llmRound) : "S";
    if (key !== curKey || cur === null) {
      cur = { key, llmRound: (s.llmRound != null ? s.llmRound : null), segs: [], rounds: [] };
      batches.push(cur);
      curKey = key;
    }
    if (s.type === "tool_use") {
      // A superseded-orphan tool_use (its round was dropped from realRounds
      // above): skip it entirely — no chip, and DON'T consume a positional
      // no-id round for it (that would mis-pair an unrelated body).
      if (s.id && _supersededTcIds.has(String(s.id))) continue;
      cur.segs.push(s);
      // Resolve the render-rich round for this tool_use.
      let r = s.id ? byId.get(String(s.id)) : null;
      if (!r && noIdCursor < noId.length) r = noId[noIdCursor++];
      if (r) cur.rounds.push(r);
    } else {
      cur.segs.push(s);
    }
  }
  if (batches.length === 0) return "";

  // If NO batch resolved any tool round (segments present but toolRounds
  // absent/unmatchable), the timeline would show prose with no tools —
  // fall back to the legacy path rather than render a lopsided view.
  const anyTool = batches.some((b) => b.rounds.length > 0);
  if (!anyTool && realRounds.length > 0) return "";

  /* Render each inject chip via its real renderer (_renderUnifiedToolLine →
   * _renderUserSteerInjectRow / peer / inbox) so the settled chip is
   * byte-identical to the live/grouped chip. */
  const _renderInjectChips = (rows) => (rows || [])
    .map((r) => _renderToolSlot(r, allRounds)).join("");

  const _emittedAnchors = new Set();
  let inner = "";
  for (const b of batches) {
    // Prepend any inject chips anchored to THIS batch's round, at the top
    // (before the batch's own thinking/narration/tools) — "user speaks first".
    if (b.llmRound != null && _injByAnchor.has(b.llmRound)) {
      inner += _renderInjectChips(_injByAnchor.get(b.llmRound));
      _emittedAnchors.add(b.llmRound);
    }
    inner += _renderTimelineBatch(b.segs, b.rounds, allRounds, idx);
  }
  // Any inject rows whose anchor round has no matching batch (e.g. a steer
  // consumed in a round that produced no tools/prose) — emit at the end so
  // the chip is never silently lost.
  for (const [anchor, rows] of _injByAnchor) {
    if (!_emittedAnchors.has(anchor)) inner += _renderInjectChips(rows);
  }
  if (!inner) return "";

  /* Wrap in the same .ptool-panel chrome so the header ("N tools used")
   * and the collapse behaviour match the legacy render exactly. The header
   * counts REAL rounds only — synthetic inject rows are not tools. */
  const anyActive = realRounds.some((r) => r.status === "searching" || r._swarmActive);
  const headerLabel = _toolPanelHeaderLabel(realRounds, anyActive);
  return `<div class="ptool-panel seg-timeline${anyActive ? " ptool-panel-active" : ""}">` +
    `<div class="ptool-panel-header"><span class="ptool-panel-label">${headerLabel}</span></div>` +
    `<div class="ptool-panel-body" data-full-count="${realRounds.length}">${inner}</div>` +
    `</div>`;
}

function _renderUnifiedGroup(allRounds, segments) {
  const anyActive = allRounds.some((r) => r.status === "searching" || r._swarmActive);
  const count = allRounds.length;
  const headerLabel = _toolPanelHeaderLabel(allRounds, anyActive);
  /* Per-round narration (translated-in-place) for the SETTLED grouped panel.
   * Empty map when no segments passed (streaming sync / branch / paper-reader
   * / upload callers) → byte-identical to the pre-fix grouped render. */
  const narrByRound = _narrationByRound(segments);
  const STATIC_LIMIT = 100;
  let lines, truncHtml = "";
  if (!anyActive && count > STATIC_LIMIT) {
    const tail = allRounds.slice(-50);
    lines = _renderToolGroupsHTML(tail, allRounds, narrByRound);
    const hiddenN = count - 50;
    truncHtml = `<div class="ptool-truncated" data-hidden-count="${hiddenN}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${escapeHtml(t("toolPanel.hidden", { n: hiddenN }))}</span></div>`;
  } else {
    lines = _renderToolGroupsHTML(allRounds, allRounds, narrByRound);
  }
  return `<div class="ptool-panel${anyActive ? " ptool-panel-active" : ""}">
       <div class="ptool-panel-header">
         <span class="ptool-panel-label">${headerLabel}</span>
       </div>
       <div class="ptool-panel-body" data-full-count="${count}">${truncHtml}${lines}</div>
     </div>`;
}

/* Collapse / expand a parallel-call group when its header is clicked.
 * Delegated (survives innerHTML rebuilds) and scoped to the header so a
 * click on a tool row inside the group never toggles it. */
document.addEventListener("click", function (e) {
  const head = e.target.closest(".ptool-turn-head");
  if (!head) return;
  const turn = head.closest(".ptool-turn");
  if (!turn) return;
  e.stopPropagation();
  const collapsed = turn.classList.toggle("collapsed");
  const chev = head.querySelector(".ptool-turn-chev");
  if (chev) chev.textContent = collapsed ? "▸" : "▾";
});

/* Peer-sender bubble → jump to the source conversation. Delegated at the
 * document level so it survives re-renders; preventDefault/stopPropagation
 * keep the surrounding <details>/<summary> from toggling on the same click.
 * The id may be the 8-char display form — resolved to the full id through the
 * shared convFullIdById seam; an unresolved id (conversation not in the loaded
 * sidebar list) gets a toast instead of a silent no-op. */
document.addEventListener("click", function (e) {
  const bubble = e.target.closest(".sw-peer-from-bubble[data-conv-jump]");
  if (!bubble) return;
  e.preventDefault();
  e.stopPropagation();
  const cid = bubble.getAttribute("data-conv-jump") || "";
  if (!cid) return;
  const fullId = (typeof convFullIdById === "function") ? convFullIdById(cid) : "";
  if (fullId && typeof loadConversation === "function") {
    loadConversation(fullId);
    return;
  }
  if (typeof showToast === "function") {
    const _t = (typeof t === "function") ? t : (k, d) => d;
    showToast("", _t("peer.convNotFoundTitle", "Conversation not found"),
              _t("peer.convNotFound", "It may have been deleted, or it is not in the current list."), 4000);
  }
});

// ★ Timer-id chip → copy the full timer id to the clipboard. Delegated at the
//   document level so it survives re-renders; the header's toggle handler
//   explicitly ignores clicks on `.timer-id-chip` so this fires cleanly.
document.addEventListener("click", function (e) {
  const chip = e.target.closest(".timer-id-chip");
  if (!chip) return;
  e.stopPropagation();
  e.preventDefault();
  const id = chip.dataset.timerId || "";
  if (!id) return;
  const done = () => {
    chip.classList.add("copied");
    const txt = chip.querySelector(".timer-id-txt");
    const _t = (typeof t === "function") ? t : (k, d) => d;
    const orig = txt ? txt.textContent : "";
    if (txt) txt.textContent = _t("timerBlock.idCopied", "Copied!");
    setTimeout(() => {
      chip.classList.remove("copied");
      if (txt) txt.textContent = orig;
    }, 1200);
  };
  if (typeof _safeClipboardWrite === "function") {
    _safeClipboardWrite(id).then(done).catch(() => {});
  } else if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(id).then(done).catch(() => {});
  }
});

// ★ Backwards compat aliases
const _renderProjectGroup = _renderUnifiedGroup;
const _renderBrowserGroup = _renderUnifiedGroup;

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
  /* PROMINENT callout shown ONLY while the login is actionable
   * (awaiting_approval) so the user knows to tap "Approve" on their
   * mobile-office app before the subprocess times out. Once the login
   * RESOLVES (approved/denied/timeout/done) this returns '' — the
   * resolution folds into the quiet turn-provenance strip instead
   * (see _mcpLoginSegment / renderTurnProvenanceHtml). */
  if (!lh) return '';
  const phase = lh.phase || 'awaiting_approval';
  if (phase !== 'awaiting_approval') return '';
  const user = lh.username || '';
  const _t = (typeof t === "function") ? t : (k => k);
  const headline = user
    ? `${_t('login.awaiting')} · ${user}`
    : _t('login.awaiting');
  const sub = _t('login.awaitingSub');
  return `<div class="mem-prefetch-chip mp-running mp-login-hint">` +
    `<span class="mp-icon">${Icon('smartphone', 14)}</span>` +
    `<span class="mp-text"><span class="mp-headline">${escapeHtml(headline)}</span>` +
    `<span class="mp-sub">${escapeHtml(sub)}</span>` +
    `</span>` +
    `<span class="mp-dots"><span>.</span><span>.</span><span>.</span></span>` +
    `</div>`;
}

/* ── Safe inline-markdown for the provenance strip ──────────────────
   The preference bullets (and some memory descriptions) carry lightweight
   markdown — `**bold**`, `*italic*`, `` `code` ``. Rendering them through a
   bare escapeHtml() showed the literal asterisks/backticks ("markdown 渲染,
   字体不好看"). We can't pipe them through the full block renderer
   (renderMarkdown wraps in <p>, runs the code-fence/table/KaTeX pipeline —
   overkill and layout-breaking for a one-line bullet). Instead: escape
   first (XSS-safe), THEN unescape only the three inline emphasis spans.
   Order matters — code spans are tokenised first so `*` inside them stays
   literal. */
function _tpInlineMd(text) {
  let s = escapeHtml(String(text == null ? "" : text));
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_m, c) => {
    codes.push(c);
    return "\x01CODE" + (codes.length - 1) + "\x02";
  });
  // Bold before italic so `**x**` isn't half-eaten by the single-* rule.
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/\x01CODE(\d+)\x02/g, (_m, i) => `<code>${codes[+i]}</code>`);
  return s;
}

/* ── Memory-prefetch segment for the unified turn-provenance strip ──
   Returns {state, segHtml, detailHtml} or null. The collapsed segment is
   icon + small count; full labels + the picked-memory list live in
   detailHtml (revealed when the strip is expanded). */
/* ── Memory-prefetch segment for the unified turn-provenance strip ──
   Returns {state, segHtml, detailHtml} or null. The collapsed segment is
   icon + small count; full labels + the picked-memory list live in
   detailHtml (revealed when the strip is expanded). */
function _memPrefetchSegment(mp) {
  if (!mp) return null;
  const phase = mp.phase || "started";
  const selected = mp.selected || 0;
  const candidates = mp.candidates || 0;
  const bm25Ms = mp.bm25Ms || 0;
  const rerankMs = mp.rerankMs || 0;
  const totalMs = mp.totalMs || 0;
  const fellBack = !!mp.fellBack;
  const _t = (typeof t === "function") ? t : (k => k);

  let icon = Icon('brain', 13);
  let state = "running";
  let count = "";        // tiny count shown beside the icon when collapsed
  let headline = "";     // full label shown in the expanded detail
  let sub = "";
  if (phase === "started") {
    headline = _t("memPrefetch.surfacing");
    sub = (mp.totalMemories ? _t("memPrefetch.totalN", { n: mp.totalMemories }) : "") + " · BM25";
  } else if (phase === "bm25_done" || phase === "rerank_started") {
    count = candidates ? String(candidates) : "";
    headline = _t("memPrefetch.filtering", { n: candidates });
    sub = `BM25 ${bm25Ms}ms`;
  } else if (phase === "done") {
    state = "done";
    if (selected === 0) {
      headline = _t("memPrefetch.none");
      sub = `${_t("memPrefetch.candidatesN", { n: candidates || 0 })} · BM25 ${bm25Ms}ms · ${_t("memPrefetch.filterLabel")} ${rerankMs}ms`;
    } else {
      count = String(selected);
      headline = _t(selected === 1 ? "memPrefetch.prefetched" : "memPrefetch.prefetchedN", { n: selected });
      const parts = [];
      if (candidates) parts.push(_t("memPrefetch.candidatesN", { n: candidates }));
      parts.push(`BM25 ${bm25Ms}ms`);
      if (rerankMs) parts.push(`${_t("memPrefetch.filterLabel")} ${rerankMs}ms`);
      if (totalMs) parts.push(`${_t("memPrefetch.totalLabel")} ${totalMs}ms`);
      if (fellBack) parts.push(_t("memPrefetch.fallback"));
      sub = parts.join(" · ");
    }
  } else if (phase === "skipped") {
    state = "skipped";
    headline = _t("memPrefetch.skipped");
    sub = mp.reason || "";
  } else if (phase === "failed") {
    state = "failed";
    icon = Icon('alertTriangle', 13);
    headline = _t("memPrefetch.failed");
    sub = mp.reason || "";
  } else {
    headline = _t("memPrefetch.generic");
    sub = phase;
  }

  // Collapsed label: a short word beside the icon so the strip reads as
  // text, not just glyphs (e.g. "3 memories", "no memories", "filtering").
  let segLabel;
  if (state === "running") segLabel = _t("memPrefetch.tag");
  else if (phase === "done" && selected > 0)
    segLabel = _t(selected === 1 ? "memPrefetch.tagN" : "memPrefetch.tagNs", { n: selected });
  else if (phase === "done") segLabel = _t("memPrefetch.tagNone");
  else if (state === "skipped") segLabel = _t("memPrefetch.tagSkipped");
  else if (state === "failed") segLabel = _t("memPrefetch.tagFailed");
  else segLabel = _t("memPrefetch.tag");

  const segHtml =
    `<span class="tp-seg tp-seg-mem tp-${state}">${icon}` +
    `<span class="tp-label">${escapeHtml(segLabel)}</span>` +
    (state === "running" ? `<span class="mp-dots"><span>.</span><span>.</span><span>.</span></span>` : "") +
    `</span>`;

  let memList = "";
  if (phase === "done" && selected > 0 && Array.isArray(mp.memories) && mp.memories.length > 0) {
    const items = mp.memories.map(m => {
      const nm = escapeHtml(m.name || "?");
      const sc = escapeHtml(m.scope || "");
      const ds = m.description ? _tpInlineMd(m.description) : "";
      return `<li><span class="mp-mem-name">${nm}</span>` +
             (sc ? ` <span class="mp-mem-scope">${sc}</span>` : "") +
             (ds ? `<div class="mp-mem-desc">${ds}</div>` : "") +
             `</li>`;
    }).join("");
    memList = `<ul class="mp-mem-list">${items}</ul>`;
  }
  const detailHtml =
    `<div class="tp-detail-row tp-detail-mem mp-${state}">` +
      `<div class="mp-text"><span class="mp-headline">${escapeHtml(headline)}</span>` +
      (sub ? `<span class="mp-sub">${escapeHtml(sub)}</span>` : "") +
      memList +
      `</div></div>`;
  return { state, segHtml, detailHtml };
}

/* ── Preferences-applied segment for the unified turn-provenance strip ──
   Backend payload: {chars, items:[...]}. See lib/memory/user_profile.py +
   EventType.PREFERENCES_APPLIED. */
function _prefsAppliedSegment(pa) {
  if (!pa) return null;
  const items = Array.isArray(pa.items) ? pa.items : [];
  const n = items.length;
  const _t = (typeof t === "function") ? t : (k => k);
  const headline = (n > 0)
    ? _t("prefs.appliedN").replace("{n}", n)
    : _t("prefs.applied");

  const segLabel = (n > 0)
    ? _t(n === 1 ? "prefs.tagN" : "prefs.tagNs", { n })
    : _t("prefs.tagNone");
  const segHtml =
    `<span class="tp-seg tp-seg-prefs tp-done">${Icon('sliders', 13)}` +
    `<span class="tp-label">${escapeHtml(segLabel)}</span>` +
    `</span>`;

  let prefList = "";
  if (n > 0) {
    const lis = items.map(it => `<li>${_tpInlineMd(it)}</li>`).join("");
    prefList = `<ul class="mp-mem-list pa-list">${lis}</ul>`;
  }
  const detailHtml =
    `<div class="tp-detail-row tp-detail-prefs pa-chip">` +
      `<div class="mp-text"><span class="mp-headline">${escapeHtml(headline)}</span>` +
      (pa.chars ? `<span class="mp-sub">${_t("prefs.fromProfile")}</span>` : "") +
      prefList +
      `</div></div>`;
  return { segHtml, detailHtml };
}

/* ── Related-conversations segment for the unified turn-provenance strip ──
   Backend payload: {count, items:[{id,title,summary}], toolsAvailable}.
   The model was told these siblings exist (ambient cross-conv awareness, see
   lib/conversations/project_summary.build_project_digest); we surface the same
   set so the user can SEE — and click into — what the model was made aware of.
   See EventType.RELATED_CONVERSATIONS. */
function _relatedConvsSegment(rc) {
  if (!rc) return null;
  const items = Array.isArray(rc.items) ? rc.items : [];
  const n = items.length || rc.count || 0;
  if (n <= 0) return null;
  const _t = (typeof t === "function") ? t : (k => k);
  const label = _t(n === 1 ? "relatedConvs.tagN" : "relatedConvs.tagNs", { n });

  const segLabel = _t(n === 1 ? "relatedConvs.tagN" : "relatedConvs.tagNs", { n });
  const segHtml =
    `<span class="tp-seg tp-seg-convs tp-done">${Icon('messageSquare', 13)}` +
    `<span class="tp-label">${escapeHtml(segLabel)}</span>` +
    `</span>`;

  let convList = "";
  if (items.length) {
    const lis = items.map(it => {
      const id = escapeHtml(it.id || "");
      const title = escapeHtml(it.title || "(untitled)");
      const summary = escapeHtml(it.summary || "");
      // Clickable when we have an id — openConversation is the global opener.
      const titleHtml = id
        ? `<a class="rc-conv-link" href="#" onclick="event.stopPropagation();try{loadConversation('${id}')}catch(e){};return false;">${title}</a>`
        : `<span class="rc-conv-title">${title}</span>`;
      return `<li>${titleHtml}` +
             (summary ? `<div class="rc-conv-summary">${summary}</div>` : "") +
             `</li>`;
    }).join("");
    convList = `<ul class="mp-mem-list rc-list">${lis}</ul>`;
  }
  const detailHtml =
    `<div class="tp-detail-row tp-detail-convs mp-done">` +
      `<div class="mp-text"><span class="mp-headline">${escapeHtml(label)}</span>` +
      `<span class="mp-sub">${escapeHtml(_t("relatedConvs.sub"))}</span>` +
      convList +
      `</div></div>`;
  return { state: "done", segHtml, detailHtml };
}

/* ── Resolved-MCP-login segment for the unified turn-provenance strip ──
   Only RESOLVED states (approved/denied/timeout/done) demote into the strip;
   while awaiting_approval the login keeps its own prominent callout (see
   renderMcpLoginHintHtml). */
function _mcpLoginSegment(lh) {
  if (!lh) return null;
  const phase = lh.phase || 'awaiting_approval';
  if (phase === 'awaiting_approval') return null;  // stays a standalone callout
  const user = lh.username || '';
  const _t = (typeof t === "function") ? t : (k => k);

  let icon, state, headline;
  if (phase === 'approved') {
    icon = Icon('check', 13); state = 'done';
    headline = user ? `${_t('login.approved')} · ${user}` : _t('login.approved');
  } else if (phase === 'denied') {
    icon = Icon('ban', 13); state = 'failed';
    headline = _t('login.denied');
  } else if (phase === 'timeout') {
    icon = Icon('alarm', 13); state = 'failed';
    headline = _t('login.timeout');
  } else {
    icon = Icon('check', 13); state = 'done';
    headline = _t('login.finished');
  }

  let snippet = '';
  if (lh.snippet && (phase === 'denied' || phase === 'timeout')) {
    let text = String(lh.snippet).trim();
    try {
      const trimmed = text.replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
      text = JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch (_e) { /* leave raw */ }
    snippet = `<pre class="mp-snippet">${escapeHtml(text)}</pre>`;
  }

  const segHtml = `<span class="tp-seg tp-seg-login tp-${state}">${icon}` +
    `<span class="tp-label">${escapeHtml(headline)}</span></span>`;
  const detailHtml =
    `<div class="tp-detail-row tp-detail-login mp-${state}">` +
      `<div class="mp-text"><span class="mp-headline">${escapeHtml(headline)}</span>` +
      snippet +
      `</div></div>`;
  return { state, segHtml, detailHtml };
}

/* ── Learned-preferences segment for the unified turn-provenance strip ──
   The layer-3 consolidation pass AUTO-APPLIES new/reinforced preference facts
   and merely INFORMS the user (kind 'added' | 'reinforced'). Those are purely
   informational — same status as a written memory — so they fold into the
   quiet collapsible strip instead of a prominent standalone box, matching how
   memory activity is surfaced. Actionable `pending` rows (propose-then-confirm,
   with Confirm/Dismiss buttons) do NOT fold in — they stay in the prominent
   renderPreferenceLearnedHtml box so the user can act on them.
   learned: [{kind:'added'|'reinforced'|'pending', summary, pending, id}]. */
function _prefsLearnedSegment(learned) {
  if (!Array.isArray(learned)) return null;
  const informational = learned.filter(p => p && !p.pending);
  if (!informational.length) return null;
  const _t = (typeof t === "function") ? t : (k => k);
  const n = informational.length;

  const segLabel = _t(n === 1 ? "prefs.learnedTagN" : "prefs.learnedTagNs", { n });
  const segHtml =
    `<span class="tp-seg tp-seg-prefs-learned tp-done">${Icon('check', 13)}` +
    `<span class="tp-label">${escapeHtml(segLabel)}</span></span>`;

  const lis = informational.map(p => {
    const lead = (p.kind === "added") ? _t("prefs.added") : _t("prefs.learnedReinforced");
    return `<li><span class="pl-seg-lead">${escapeHtml(lead)}</span> ${_tpInlineMd(p.summary || "")}</li>`;
  }).join("");
  const detailHtml =
    `<div class="tp-detail-row tp-detail-prefs-learned mp-done">` +
      `<div class="mp-text"><span class="mp-headline">${escapeHtml(_t("prefs.learnedHeadline"))}</span>` +
      `<span class="mp-sub">${escapeHtml(_t("prefs.editInSettings"))}</span>` +
      `<ul class="mp-mem-list pa-list">${lis}</ul>` +
      `</div></div>`;
  return { state: "done", segHtml, detailHtml };
}

/* ── UNIFIED turn-provenance strip ───────────────────────────────────
   Replaces the old stack of separate boxes (memory-prefetch +
   preferences-applied + resolved login) with ONE quiet, collapsible
   strip at the head of the assistant turn. Collapsed = icons + small
   counts; click anywhere on the strip → expands to show full labels,
   the picked-memory list, and which preferences were in context.
   The awaiting-approval login callout is rendered SEPARATELY and stays
   prominent (see renderMcpLoginHintHtml) — only resolved logins fold in
   here as a small segment.
   Called from both the streaming path (streaming_ui.js) and the
   finished-message path (chat_render.js) so the strip is identical
   live and after reload. */
function renderTurnProvenanceHtml(msg) {
  if (!msg) return "";
  const segs = [_mcpLoginSegment(msg._mcpLoginHint),
                _memPrefetchSegment(msg._memoryPrefetch),
                _prefsAppliedSegment(msg._preferencesApplied),
                _prefsLearnedSegment(msg._preferencesLearned),
                _relatedConvsSegment(msg._relatedConversations)]
                .filter(Boolean);
  if (segs.length === 0) return "";

  const running = segs.some(s => s.state === "running");
  const failed = segs.some(s => s.state === "failed");
  const stripState = failed ? "tp-has-failed" : (running ? "tp-running" : "tp-done");
  const segHtml = segs.map(s => s.segHtml).join("");
  const detailHtml = segs.map(s => s.detailHtml).join("");

  return `<div class="turn-prov ${stripState} tp-expandable" onclick="this.classList.toggle('tp-expanded')">` +
    `<span class="tp-segs">${segHtml}</span>` +
    `<span class="tp-chevron">${Icon('chevronDown', 12)}</span>` +
    `<div class="tp-details">${detailHtml}</div>` +
    `</div>`;
}

/* ── Back-compat shims ───────────────────────────────────────────────
   Older call sites / persisted-message render paths may still reference
   the per-chip builders. Route them through the unified strip so a stray
   caller never resurrects the old stacked boxes. */
function renderMemoryPrefetchHtml(mp) {
  return mp ? renderTurnProvenanceHtml({ _memoryPrefetch: mp }) : "";
}
function renderPreferencesAppliedHtml(pa) {
  return pa ? renderTurnProvenanceHtml({ _preferencesApplied: pa }) : "";
}

function renderPreferenceLearnedHtml(learned) {
  /* "Remembered: X" moment(s) from the layer-3 consolidation pass.
     learned: [{kind:'added'|'reinforced'|'pending', summary, pending, id}].
     New/reinforced preferences are AUTO-APPLIED and purely INFORMATIONAL —
     same status as a written memory — so they now fold into the quiet
     turn-provenance strip (_prefsLearnedSegment), NOT this box. This box only
     renders ACTIONABLE `pending` rows (the legacy propose-then-confirm gate
     carried by old persisted messages) so the Confirm/Dismiss affordance stays
     prominent. Backed by EventType.PREFERENCE_LEARNED. */
  if (!Array.isArray(learned) || !learned.length) return "";
  const _t = (typeof t === "function") ? t : (k => k);
  const rows = learned.filter(p => p && p.pending).map(p => {
    const sum = escapeHtml(p.summary || "");
    const pid = escapeHtml(p.id || "");
    return `<div class="pl-row pl-pending" data-pref-id="${pid}">` +
      `<span class="pl-lead">${Icon('lightbulb', 13)}</span>` +
      `<span class="pl-text">${_t("prefs.learned")} <b>${sum}</b>` +
      `<span class="pl-hint">${_t("prefs.pendingHint")}</span></span>` +
      `<span class="pl-actions">` +
      `<button class="pl-btn pl-confirm" onclick="window.resolvePreference&&resolvePreference(this,'${pid}',true)">${_t("prefs.confirm")}</button>` +
      `<button class="pl-btn pl-dismiss" onclick="window.resolvePreference&&resolvePreference(this,'${pid}',false)">${_t("prefs.dismiss")}</button>` +
      `</span></div>`;
  }).join("");
  if (!rows) return "";
  return `<div class="pref-learned-box">${rows}</div>`;
}

/* A "superseded" orphan round: an early-announced tool_start that was left
 * result-less when a discarded FloorRetry / stream-retry attempt's tc_id never
 * survived into the final assistant_msg, then settled by the backend
 * reconcile_announced_rounds (badge='superseded', interrupted=true, NO real
 * result). It is pure noise — its adopted twin (or the recovered response) is
 * the real call — so we DROP it from the render entirely rather than show a
 * misleading "interrupted" chip for a call the user never actually lost.
 *
 * NOT dropped: a genuine user-Stop dangling round (badge='interrupted', from
 * _finalize_dangling_tool_rounds) — the user really interrupted that one, so it
 * keeps its static interrupted affordance. Discriminator = the 'superseded'
 * badge, which ONLY reconcile_announced_rounds stamps. */
function _isSupersededOrphanRound(r) {
  if (!r) return false;
  const meta = (r.results && r.results[0]) || {};
  // result-less: reconcile writes a single meta with no tool content/toolContent
  const hasRealResult = r.toolContent != null
    || (meta && (meta.fetched || (meta.fetchedChars | 0) > 0));
  // ★ Authoritative signal is the 'superseded' badge (ONLY
  //   reconcile_announced_rounds stamps it) + result-less — NOT the status.
  //   The status intentionally DIFFERS between the two apply paths for the
  //   SAME husk: the backend stamps entry.status='aborted' locally (→ what the
  //   persisted/reloaded snapshot carries), but the live tool_result SSE event
  //   the reconcile emitted carries no status, so the pure reducer's
  //   'tool_result' case settles the live round to status='done'. Gating on
  //   status==='aborted' (the old guard) therefore dropped the husk ONLY after
  //   the done-event/reload rewrote it to 'aborted' — the live in-turn round
  //   stayed 'done' and rendered a misleading "interrupted"/"superseded" chip
  //   for the whole rest of the turn. Keying on badge+result-less drops it on
  //   BOTH paths. A still-in-flight round (status 'searching'/'executing') has
  //   results=null → meta={} → badge undefined → correctly NOT dropped. */
  return meta.badge === "superseded" && !hasRealResult;
}

function renderToolRoundsHTML(rounds, isStreaming, segments) {
  if (!rounds || rounds.length === 0) return "";
  /* ★ Drop superseded orphan rounds (FloorRetry/stream-retry duplicates left
   *   result-less and reconciled) so they never render a misleading
   *   "interrupted" chip. The user's real call is the adopted/recovered twin.
   *   Genuine user-Stop interruptions (badge='interrupted') are kept. */
  rounds = rounds.filter((r) => !_isSupersededOrphanRound(r));
  if (rounds.length === 0) return "";
  /* ★ UNIFIED: every round — tool calls AND swarm panels — goes into
   *   the single ptool-panel in chronological order. Swarm rounds
   *   render the full agent dashboard inline as a "row" so the user
   *   sees the order in which the main agent issued spawn_agents,
   *   await_agents, get_agent_result, and any other tools, all in
   *   one timeline.
   *   `segments` (optional): when the settled assistant message carries the
   *   backend segment list, the grouped panel renders each round's narration
   *   (translated-in-place) adjacent to its tools — so a translated turn does
   *   NOT clump its narration into one tail block when the segment-timeline
   *   toggle is OFF (or the timeline path fell back to grouped). */
  return _renderUnifiedGroup(rounds, segments);
}

/* ── 1 Hz wall-clock ticker for the run_command countdown / elapsed chip ──
 * Third instance of the same pattern as _tickTimerCountdowns and
 * _tickSwarmTimers: the text changes every second even when no SSE event
 * landed, and the fingerprint gate in _syncToolRoundsDOM (correctly) skips
 * re-renders when nothing changed — so without a ticker the chip would freeze
 * at whatever value it was first painted with.
 *
 * Updates [data-cmd-timer] elements IN PLACE: zero re-render, one timer,
 * O(N running commands) per tick. Reads only server clocks off the DOM
 * attributes, so it stays truthful across a reconnect. */
function _tickCmdTimers() {
  const els = document.querySelectorAll('.ptool-cmd-timer[data-cmd-timer]');
  if (!els.length) return;
  const now = Date.now();
  for (const el of els) {
    const a = el.getAttribute('data-cmd-anchor');
    const d = el.getAttribute('data-cmd-deadline');
    const st = _cmdTimerState({
      execStartTs: a ? +a : null,
      deadlineTs: d ? +d : null,
    }, now);
    if (!st) continue;
    if (el.textContent !== st.txt) el.textContent = st.txt;
    const over = st.cls.indexOf('over') >= 0;
    const soon = st.cls.indexOf('soon') >= 0;
    if (el.classList.contains('ptool-cmd-timer-over') !== over) el.classList.toggle('ptool-cmd-timer-over', over);
    if (el.classList.contains('ptool-cmd-timer-soon') !== soon) el.classList.toggle('ptool-cmd-timer-soon', soon);
  }
}
if (typeof window !== 'undefined' && !window._cmdTimerTicker) {
  window._cmdTimerTicker = setInterval(() => {
    try {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
      _tickCmdTimers();
    } catch (e) { /* swallowed — countdown ticker is best-effort */ }
  }, 1000);
}

/* ── Lazy thinking expand ────────────────────────────────
   Don't dump 30-100k+ chars of thinking text into the DOM
   on every render — inject it only when the user expands.
   This prevents DevTools / Elements tab from choking.      */

