/* ===== migrated source: ui/tool_rounds.js ===== */
/* Tool-round rendering for search, code, project, browser, media, and swarm.
 * Runtime composition concatenates this file into the shared browser scope. */

/* Command-output toggles must also render in the isolated degraded harness,
 * where the global icon registry is intentionally absent. */
const _PT_CHEVRON_DOWN_SVG = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true"><path d="m5 6.5 3 3 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const _PT_CHEVRON_RIGHT_SVG = TOOL_ROUND_CHEVRON_RIGHT_SVG;

/* Temporary SVG adapter for the two retained sibling renderers. Labels,
 * colors, and icon selection belong to the pure typed presentation owner. */
const _TOOL_DISPLAY = Object.freeze(Object.fromEntries(
  EXPLICIT_TOOL_ROUND_DISPLAY_NAMES.map((name) => {
    const display = explicitToolRoundDisplay(name);
    if (!display) throw new Error(`missing tool display contract: ${name}`);
    return [name, Object.freeze({
      icon: display.iconName ? Icon(display.iconName) : "",
      label: display.label,
      color: display.color,
    })];
  }),
));
const _getToolDisplay = toolRoundDisplay;
// ═══════════════════════════════════════════
//  Unified Tool Activity Panel
// ═══════════════════════════════════════════

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

/* Native Responses multi-agent attribution. Local Swarm already owns a rich
 * agent dashboard; native workers return ordinary tool rounds with
 * caller={type:'multi_agent',agent_name}. Keep that identity on every row so
 * a real delegated execution is visible instead of looking like a root call. */
function _renderNativeAgentPill(round, noColon) {
  const caller = round && round.caller;
  if (!caller || caller.type !== "multi_agent") return "";
  const full = String(caller.agent_name || "");
  if (!full || full === "/root") return "";
  const label = full.split("/").filter(Boolean).pop() || full;
  const sep = noColon ? "" : ":";
  return `<span class="ptool-root" title="Native multi-agent worker: ${escapeHtml(full)}">agent ${escapeHtml(label)}${sep}</span>`;
}


/**
 * Render the "auto-fixed" badge shown when the harness repaired a tool
 * call's malformed arguments before executing it (e.g. recovered truncated
 * JSON, or coerced a stringified array). `round._repaired` is
 * `{label, detail, patterns}` emitted by lib/tasks_pkg/tool_dispatch/_pipeline.py.
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
  return `<span class="ptool-badge ${cls}" title="${tip}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/></svg> ${label}</span>${toolResultPresentation.renderRepairKindsHtml(round)}`;
}

function _toolRejectionDescriptor(round) {
  const meta = (round && Array.isArray(round.results) ? round.results : [])[0] || {};
  return (round && (round.rejection || round._rejected))
    || meta.rejection || meta.rejected || meta._rejected || {};
}

/* A rejected call never ran, so its ARGUMENTS are the only record of what the
 * model actually sent — the reason names the rule it broke, not the payload
 * that broke it (a blank `edits[0].path` reads identically whether the model
 * omitted the file or the harness dropped it). Show the attempt under the
 * reason: inline when it is short enough to read at a glance, one click (with
 * its size) when it carries whole file bodies. */
const REJECTED_ARGS_INLINE_CHARS = 600;
const REJECTED_ARGS_VIEW_CHARS = 4000;

function _rejectedCallArgsHtml(round, tr) {
  const raw = round ? round.toolArgs : null;
  let text = "";
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return "";
    try {
      const parsed = JSON.parse(trimmed);
      if (!parsed || typeof parsed !== "object") return "";
      if (!Array.isArray(parsed) && !Object.keys(parsed).length) return "";
      text = JSON.stringify(parsed, null, 2);
    } catch (_) { text = trimmed; }
  } else if (raw && typeof raw === "object" && !Array.isArray(raw)
             && Object.keys(raw).length) {
    try { text = JSON.stringify(raw, null, 2); } catch (_) { return ""; }
  }
  if (!text) return "";
  const clipped = text.length > REJECTED_ARGS_VIEW_CHARS;
  if (clipped) text = text.slice(0, REJECTED_ARGS_VIEW_CHARS);
  const label = escapeHtml(tr("tool.requestParams", "request parameters"));
  const open = text.length <= REJECTED_ARGS_INLINE_CHARS ? " open" : "";
  const size = Math.ceil(text.length / 1024);
  const more = clipped
    ? `<span class="ptool-reject-args-more">${escapeHtml(tr(
        "tool.requestParamsTruncated",
        "… truncated to %d KB; the wire keeps the full arguments"
      ).replace("%d", String(Math.round(REJECTED_ARGS_VIEW_CHARS / 1024))))}</span>`
    : "";
  const count = open
    ? ""
    : `<span class="ptool-rejected-params-size">${size} KB</span>`;
  return `<details class="ptool-rejected-params"${open}>
       <summary>${label}${count}</summary>
       <pre class="ptool-reject-args">${escapeHtml(text)}</pre>${more}
     </details>`;
}
/* Typed cause separates a missing tool from a valid pre-execution block. */
function _renderRejectedToolLine(round, svg) {
  const meta = (round.results || [])[0] || {};
  const rej = _toolRejectionDescriptor(round);
  const kind = String(rej.kind || "");
  const isHallucinated = kind === "hallucinated";
  const attempted = isHallucinated
    ? (rej.attempted || round.toolName || "?")
    : (round.query || rej.tool || rej.attempted || round.toolName || "?");
  const sugg = Array.isArray(rej.suggestions) ? rej.suggestions.filter(Boolean) : [];
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const badgeLabel = escapeHtml(isHallucinated
    ? _t("tool.hallucinated", "not a real tool")
    : _t("tool.blocked", "blocked"));
  const fallbackTip = isHallucinated
    ? _t("tool.hallucinatedTip", "The model called a tool that doesn't exist this turn — it was rejected and never run.")
    : _t("tool.blockedTip", "A safety or authority check blocked this valid tool before execution.");
  const rawReason = [rej.reason, round.toolContent, meta.content, meta.reason]
    .find((value) => typeof value === "string" && value.trim()) || "";
  /* The clipped tail ("…cursor(str…") is exactly the part that explains what
   * to fix — render the reason whole, line breaks included. */
  let fullReason = rawReason.trim();
  const kindCode = String(rej.kind || "").trim();
  if (kindCode && kindCode !== "hallucinated" && !fullReason.includes(kindCode)) {
    fullReason = fullReason ? `${fullReason}\n${kindCode}` : kindCode;
  }
  const reasonHtml = escapeHtml(fullReason).replace(/\n/g, "<br>");
  const tip = escapeHtml(rawReason || fallbackTip);
  let suggHtml = "";
  if (isHallucinated && sugg.length) {
    const chips = sugg.map((s) => `<code class="ptool-reject-sugg">${escapeHtml(s)}</code>`).join(" ");
    const did = escapeHtml(_t("tool.didYouMean", "did you mean"));
    suggHtml = `<span class="ptool-reject-hint">${did} ${chips}?</span>`;
  }
  /* SVG glyph (§3.4 — no emoji): a circle-slash "forbidden" mark. */
  const banSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><circle cx="12" cy="12" r="9"/><line x1="5.6" y1="5.6" x2="18.4" y2="18.4"/></svg>`;
  const argsHtml = _rejectedCallArgsHtml(round, _t);
  return `<div class="ptool-line ptool-rejected" title="${tip}">
       <span class="ptool-icon">${svg}</span>
       <span class="ptool-text${isHallucinated ? " ptool-reject-name" : ""}">${escapeHtml(attempted)}</span>
       <span class="ptool-badge ptool-badge-reject">${banSvg} ${badgeLabel}</span>
       ${suggHtml}
       ${reasonHtml ? `<span class="ptool-error-reason">${reasonHtml}</span>` : ""}${argsHtml ? `\n       ${argsHtml}` : ""}
     </div>`;
}

/* Synthetic context-injection presentation lives in the typed owner. */
/* Memory preview card — create_memory / update_memory / merge_memories.
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

/* Checklist history is a pure typed projection. The retained dispatcher only
 * coordinates the context rail; typed checklist-card owns each audit card. */
function _projectTodoRoundsForDisplay(rounds) {
  return projectChecklistHistory(Array.isArray(rounds) ? rounds : []);
}

/* Rich conv-meta, checklist, motion, and Timer Watcher renderers live in the
 * immediately following retained section, ui/tool_rounds_rich.js. The
 * typeof guards preserve a safe core-only test/evolution seam. */

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
  return renderMcpResourceLinks(text, round);
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
      /* Semantic key shapes beat a raw first-string fragment: a recovery row
       * should say WHAT ran ('$ npm test', '"timeout"', 'src/app.ts'), not
       * dump an arbitrary argument slice. */
      const keys = Object.keys(args);
      const firstString = (k) => {
        const v = args[k];
        return (typeof v === 'string' && v.trim()) ? v.trim().split('\n')[0].slice(0, 80) : '';
      };
      for (const k of keys) {
        const v = firstString(k);
        if (!v) continue;
        const lk = k.toLowerCase();
        if (lk === 'command' || lk === 'cmd') { summary = '$ ' + v; break; }
        if (lk === 'query' || lk === 'pattern' || lk === 'q') { summary = '"' + v + '"'; break; }
        if (lk === 'path' || lk === 'file' || lk === 'filepath' || lk === 'file_path'
            || lk === 'workdir' || lk === 'cwd') { summary = v; break; }
      }
      if (!summary) {
        for (const k of keys) {
          const v = firstString(k);
          if (v) { summary = v; break; }
        }
      }
    }
  } catch (e) { /* malformed toolArgs — the label alone still beats a blank row */ }
  return escapeHtml(summary ? base + ' — ' + summary : base);
}

function _renderUnifiedToolLine(round, isSearching) {
  // Protocol-only adapter: its real children render as ordinary tool rows.
  if (round && round.toolName === "execute_tools") return "";
  const svg = toolRoundSvg(round);
  const td = _getToolDisplay(round);
  /* Preserve real newlines in the tool-call title — batch search/fetch
   * displays render one item per line so users can see every candidate
   * without elision. escapeHtml first (HTML-safe), THEN substitute
   * \n → <br> so the browser actually breaks the line. */
  const query = toolHeaderQueryText(round);
  const q = renderRepoMapHeaderHtml(round) || renderToolArtifactHeaderHtml(round)
    || (query ? _linkifyMcpLabels(escapeHtml(query).replace(/\n/g, '<br>'), round)
      : _recoveryRoundFallbackTitle(round, td));
  const results = round.results || [];
  const meta = results[0] || {};
  const rootPill = _renderNativeAgentPill(round) + _renderToolRootPill(round);
  /* run_command / code_exec render the root prefix inline on the title
   * line (alongside the description), where a trailing colon reads as if
   * it introduces the description — so the command-header variant drops it. */
  const cmdRootPill = _renderNativeAgentPill(round, true)
    + _renderToolRootPill(round, true);
  /* Harness self-repair badge — the backend auto-corrected this call's
   *   malformed arguments (truncated/invalid JSON, or schema-shape coercion).
   *   Surfacing it tells the user the displayed/executed args differ from
   *   the model's raw (broken) output. */
  const repairedBadge = _renderToolRepairedBadge(round);
  /* Shared locals handed to the per-branch renderer helpers below — built
   * once; each helper destructures only what it needs so the moved code
   * stays byte-identical to the pre-split inline branches. */
  const ctx = { svg, td, q, results, meta, rootPill, cmdRootPill, repairedBadge, isSearching };

  // Synthetic context-injection lanes share one pure typed presentation
  // owner. Retained code owns chronology and dispatch order only.
  const injectionHtml = renderToolInjectionHtml(round);
  if (injectionHtml) return injectionHtml;

  // The typed cause, never status alone, selects the rejection presentation.
  if (round.status === "rejected" && _toolRejectionDescriptor(round).kind) {
    return _renderRejectedToolLine(round, svg);
  }

  // Human Guidance normalization, state priority, bounds, and action strings
  // belong to one pure typed owner. Retained code supplies trusted chrome.
  const guidanceHtml = renderToolHumanGuidanceHtml(round, {
    iconHtml: svg,
    toolDisplayLabel: td && td.label,
  });
  if (guidanceHtml) return guidanceHtml;

  // Pending approval state — show approve/reject buttons
  const approvalHtml = renderToolApprovalHtml(round, {
    iconHtml: svg,
    queryHtml: q,
  });
  if (approvalHtml) return approvalHtml;

  // Timer Watcher: render collapsible poll checks from the adjacent rich
  // section. A core-only materialization falls through to the generic line.
  const recoveredTimer = _timerRecoveryPresentation(round);
  if (((recoveredTimer.polls && recoveredTimer.polls.length > 0) || round._timerSkipCount
        || (round.toolName === "timer_create" && round.results?.some(result => result.timerReceipt)))
      && typeof _renderTimerWatcherBlock === 'function') {
    return _renderTimerWatcherBlock(round, svg);
  }
  // Timer tool with "searching" status but no polls yet — show initial waiting
  // After reconnection, backend now includes _timerPolls in state snapshots,
  // so this state should be brief (only before the first poll fires).
  const timerWaitHtml = _renderTimerWaitingRow(round, ctx);
  if (timerWaitHtml) return timerWaitHtml;

  // Interactive stdin: subprocess is waiting for user keyboard input
  const stdinHtml = _renderStdinBlock(round, ctx);
  if (stdinHtml) return stdinHtml;

  // Result-less aborted rounds are terminal and must never retain a spinner.
  const abortedHtml = _renderAbortedRow(round, ctx);
  if (abortedHtml) return abortedHtml;

  // Probe error before searching/done so a failed tool cannot look successful.
  const errorHtml = _renderErrorRow(round, ctx);
  if (errorHtml) return errorHtml;

  const searchingHtml = _renderSearchingRow(round, ctx);
  if (searchingHtml) return searchingHtml;

  const commandInteraction = {
    bodyExpanded: Boolean(round.toolCallId
      && _cmdBodyExpanded.has(_cmdInteractionKey(round))),
    outputExpanded: Boolean(round.toolCallId
      && _cmdOutputExpanded.has(_cmdInteractionKey(round))),
  };
  const commandHeader = {
    iconHtml: svg,
    rootPillHtml: cmdRootPill,
    timerHtml: '',
    interruptHtml: '',
    rightControlsHtml: _rowRightControls(round),
  };
  const cmdDoneHtml = renderSettledToolCommandHtml(
    round,
    meta,
    commandHeader,
    commandInteraction,
  );
  if (cmdDoneHtml) return cmdDoneHtml;

  const browserExecutionHtml = renderToolBrowserExecutionHtml(round, meta, {
    iconHtml: svg,
    rootPillHtml: cmdRootPill,
    rightControlsHtml: _rowRightControls(round),
  }, commandInteraction);
  if (browserExecutionHtml) return browserExecutionHtml;

  const toolSearchHtml = renderToolSearchHtml(round, results, {
    iconHtml: svg,
    queryHtml: q,
    rightControlsHtml: _rowRightControls(round),
  });
  if (toolSearchHtml) return toolSearchHtml;

  const toolImageHtml = renderToolImageHtml(round, meta, {
    iconHtml: svg,
    queryHtml: q,
    rightControlsHtml: _rowRightControls(round),
  });
  if (toolImageHtml) return toolImageHtml;

  // Header policy is shared by fresh and historical rounds in the typed owner.
  const badgeHtml = renderToolHeaderBadgeHtml(round, meta);

  const compactionLabelHtml = renderToolResultCompactionLabelHtml(round);
  // create_memory / update_memory / merge_memories — collapsible,
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

  // todo_write — collapsible checklist progress card (state glyphs + a slim
  //   progress bar), rendered off the structured meta.todos the backend
  //   attaches. Falls through to the generic line only if the list is absent.
  if (round.toolName === "todo_write") {
    /* Side channel for the per-turn context rail: every accepted todo_write
     * rewrites the live checklist, so the rail's checklist row follows the
     * plan in real time instead of waiting for the turn to settle. Read the
     * same structured meta the card renders; a rejected/no-op round carries
     * the authoritative list too (the backend attaches it on every outcome),
     * so the rail never regresses to an older plan. */
    if (typeof runtimeScope.updateTurnCtxTodos === 'function') {
      const _liveTodos = Array.isArray(meta.todos) ? meta.todos
        : (Array.isArray(round.todos) ? round.todos : null);
      if (_liveTodos) {
        try { runtimeScope.updateTurnCtxTodos(_liveTodos); }
        catch (_e) { /* rail sync must never break the tool render */ }
      }
    }
    const todoHtml = (typeof _renderTodoBlock === "function")
      ? _renderTodoBlock(round, svg, q, badgeHtml) : "";
    if (todoHtml) return todoHtml;
  }

  // The typed result owner selects write_file, single-diff, and batch-edit
  // presentation from the exact projected round + first result metadata.
  const writeResultHtml = renderWriteToolResultHtml(round, meta, {
    iconHtml: svg,
    queryHtml: q,
    rootPillHtml: rootPill,
    badgeHtml,
  });
  if (writeResultHtml) return writeResultHtml;

  // Motion-video / produce tools — structured result card (per-scene
  //   narration table, probe specs, gate errors, mux duration, …) instead of
  //   the bare name+badge line that hid everything the pipeline reported.
  //   Core-only materializations degrade through the typeof guard.
  if (isMotionToolRound(round) && round.status === "done") {
    const motionHtml = (typeof _renderMotionVideoBlock === 'function')
      ? _renderMotionVideoBlock(round, svg, q, badgeHtml) : "";
    if (motionHtml) return motionHtml;
  }

  // Project-brain / conversation-meta tools — render their full prose
  //   output in a collapsible Markdown card instead of the bare generic line
  //   (which hid all the content). Only when the round has settled (done);
  //   the in-flight "searching…" state is handled by the generic active
  //   branch above.
  // A historical-conversation read must stay on the generic authoritative
  // result path. Older snapshots may carry a `convDigest` built by a second DB
  // read; using it as the body can show a different page/revision than the
  // settled `toolContent` (the observed repeated-card failure).
  if (isConversationMetadataToolRound(round)
      && round.toolName !== "get_conversation"
      && round.status !== "rejected") {
    // A core-only materialization degrades through this typeof guard.
    const convMetaHtml = (typeof _renderConvMetaBlock === 'function')
      ? _renderConvMetaBlock(round, svg, q, badgeHtml) : "";
    if (convMetaHtml) return convMetaHtml;
  }

  // Catch-all: any other settled round with a real result body (read_files,
  //   grep_search, find_files, list_dir, browser reads, MCP tools, …) renders
  //   as an expandable result viewer instead of the bare line below.
  const genericResultHtml = renderGenericToolResultHtml(round, meta, {
    iconHtml: svg,
    queryHtml: q,
    rootPillHtml: rootPill,
    badgeHtml,
    repairedBadgeHtml: repairedBadge,
    rightControlsHtml: _rowRightControls(round),
    toolDisplayLabel: td && td.label,
  });
  if (genericResultHtml) return genericResultHtml;

  /* Minimal settle chip for the bare-line catch-all: a round that "did
   * something" without a result body used to render as a dead label. A
   * solid-glyph state chip (never opacity) says how it ended. */
  const _bareStatus = String(round.status || "");
  const _bareChip = isSearching || !_bareStatus ? ""
    : (_bareStatus === "done"
      ? '<span class="ptool-line-status ptool-line-status--ok" title="done">✓</span>'
      : `<span class="ptool-line-status ptool-line-status--err" title="${escapeHtml(_bareStatus)}">${escapeHtml(_bareStatus)}</span>`);

  return `<div class="ptool-line">
       <span class="ptool-icon">${svg}</span>
       ${compactionLabelHtml}
       ${rootPill}
       <span class="ptool-text">${q}</span>
       ${repairedBadge}
       ${badgeHtml}
       ${_bareChip}
       ${_rowRightControls(round)}
     </div>`;
}

/* ══════════════════════════════════════════════════════════════════════════
 * Per-branch renderers for _renderUnifiedToolLine. Each helper guards on its
 * own trigger condition and returns "" when the branch does not apply, so the
 * dispatcher stays a flat ordered list of `if (html) return html;` probes —
 * the probe ORDER is the render priority and must not change.
 * ══════════════════════════════════════════════════════════════════════════ */

// Timer tool with "searching" status but no polls yet — show initial waiting
// After reconnection, backend now includes _timerPolls in state snapshots,
// so this state should be brief (only before the first poll fires).
const _timerPollRecoveryById = new Map();
const _timerPollRecoveryAttempted = new Set();
const _TIMER_POLL_RECOVERY_LIMIT = 256;

function _claimTimerPollRecovery(timerId) {
  if (!timerId || _timerPollRecoveryAttempted.has(timerId)) return false;
  while (_timerPollRecoveryAttempted.size >= _TIMER_POLL_RECOVERY_LIMIT) {
    const oldestTimerId = _timerPollRecoveryAttempted.values().next().value;
    if (!oldestTimerId) break;
    _timerPollRecoveryAttempted.delete(oldestTimerId);
    _timerPollRecoveryById.delete(oldestTimerId);
  }
  _timerPollRecoveryAttempted.add(timerId);
  return true;
}

function _timerRecoveryPresentation(round) {
  const recovered = round?._timerTimerId
    ? _timerPollRecoveryById.get(round._timerTimerId) : null;
  return {
    polls: round?._timerPolls || recovered?.polls || [],
    triggered: Boolean(round?._timerTriggered || recovered?.triggered),
  };
}
function _renderTimerWaitingRow(round, ctx) {
  const { q } = ctx;
  if (!(round.toolName === "timer_create" && round.status === "searching"
      && !_timerRecoveryPresentation(round).polls.length)) return "";
  // Try to recover timer polls from the API if timerId is known
  if (_claimTimerPollRecovery(round._timerTimerId)) {
    _recoverTimerPolls(round);
  }
  return `<div class="ptool-line ptool-active">
         <span class="ptool-icon">${Icon('timer')}</span>
         <span class="ptool-text">${q || escapeHtml(t('timerBlock.watcherTitle'))}</span>
         <span class="ptool-badge ptool-badge-warn">${escapeHtml(t('timerBlock.waitingFirstPoll'))}</span>
         <span class="ptool-spinner"></span>
       </div>`;
}

// Interactive stdin: subprocess is waiting for user keyboard input
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
                    data-tofu-action-keydown="if(event.key==='Enter'){event.preventDefault();submitStdinInput('${sid}',this.value)}" />
             <button class="stdin-submit-btn" data-tofu-action="submitStdinInput('${sid}', document.getElementById('stdin-${sid}').value)"
                     title="Send input">
               <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
             </button>
             <button class="stdin-eof-btn" data-tofu-action="submitStdinEof('${sid}')" title="Send EOF (close stdin)">
               EOF
             </button>
           </div>
         </div>
       </div>`;
}

// Interrupted — the task was aborted (Stop) while this tool round was
//   still in-flight. The backend dangling-round sweep
//   (orchestrator._finalize_dangling_tool_rounds) stamps status='aborted'
//   on rounds the abort short-circuit left in 'searching'. Render a static
//   "interrupted" affordance — NO spinner — so it never shows "Running…"
//   live or after reload. Real results (if any) fall through to the normal
//   done renderers below, since the sweep only marks result-less rounds.
function _renderAbortedRow(round, ctx) {
  const { svg, td, meta, rootPill } = ctx;
  if (!(round.status === "aborted" && !(round.results && round.results.length && !meta.interrupted))) return "";
  const cmdText = renderRepoMapHeaderHtml(round)
    || escapeHtml(round.query || meta.title || td.label || round.toolName || "");
  return `<div class="ptool-line ptool-interrupted" data-rn="${round.roundNum}">
         <span class="ptool-icon">${svg}</span>
         ${rootPill}
         <span class="ptool-text">${cmdText}</span>
         <span class="ptool-badge ptool-badge-interrupted">interrupted</span>
       </div>`;
}

/* Batch-edit tools whose failed verdict renders as the typed owner's compact
 * outcome card (per-edit rows + per-kind chips) instead of the raw log dump
 * below. Mirrors EDIT_FAILURE_TOOL_NAMES in tool-result-presentation.ts. */
const _EDIT_FAILURE_TOOL_NAMES = new Set([
  "edit_file", "apply_diff", "apply_diffs", "insert_content", "insert_contents",
]);

/* Terminal tool errors show the first model-visible reason inline, with no
 * spinner or success badge. */
function _renderErrorRow(round, ctx) {
  const { svg, td, meta, rootPill, q } = ctx;
  if (round.status !== "error") return "";

  const browserHtml = renderToolBrowserExecutionHtml(round, meta, {
    iconHtml: svg, rootPillHtml: ctx.cmdRootPill,
    rightControlsHtml: _rowRightControls(round),
  }, {
    bodyExpanded: Boolean(round.toolCallId && _cmdBodyExpanded.has(_cmdInteractionKey(round))),
    outputExpanded: Boolean(round.toolCallId && _cmdOutputExpanded.has(_cmdInteractionKey(round))),
  });
  if (browserHtml) return browserHtml;
  // Artifact errors keep the same sole disclosure and visible failure verdict.
  if (renderToolArtifactHeaderHtml(round)) {
    const artifactHtml = renderGenericToolResultHtml(round, meta, {
      iconHtml: svg, queryHtml: q, rootPillHtml: rootPill,
      badgeHtml: renderToolHeaderBadgeHtml(round, meta),
      repairedBadgeHtml: ctx.repairedBadge, rightControlsHtml: _rowRightControls(round),
      toolDisplayLabel: td && td.label,
    });
    if (artifactHtml) return artifactHtml;
  }
  /* Same linkify as the done lane: a failed read_doc still renders its doc
   * title as a clickable link — failure must not strip navigation. */
  const cmdText = q || _linkifyMcpLabels(
    escapeHtml(toolHeaderQueryText(round) || meta.title || td.label || round.toolName || "").replace(/\n/g, "<br>"),
    round);
  // Keep failure disclosure in the typed write owner. Remove this bridge
  // with the classic error renderer when tool rows finish migrating.
  if (round.toolName === "write_file" || _EDIT_FAILURE_TOOL_NAMES.has(round.toolName)) {
    const writeCardHtml = renderWriteToolResultHtml(round, meta, {
      iconHtml: svg, queryHtml: cmdText, rootPillHtml: rootPill,
      badgeHtml: renderToolHeaderBadgeHtml(round, meta),
    });
    if (writeCardHtml) return writeCardHtml;
  }
  // Compatibility bridge only: typed result presentation owns send-message
  // content/errors. Delete with this legacy error-row dispatcher migration.
  if (round.toolName === "send_agent_message") {
    const sentMessageHtml = renderGenericToolResultHtml(round, meta, {
      iconHtml: svg, queryHtml: q, rootPillHtml: rootPill, badgeHtml: "",
      repairedBadgeHtml: ctx.repairedBadge,
      rightControlsHtml: _rowRightControls(round),
    });
    if (sentMessageHtml) return sentMessageHtml;
  }
  // One authoritative/deferred disclosure, with bounded safe header copy.
  // Delete this bridge with the classic dispatcher; never replay the tool.
  return renderGenericToolResultHtml(round, meta, {
    iconHtml: svg, queryHtml: cmdText, rootPillHtml: rootPill,
    badgeHtml: renderToolHeaderBadgeHtml(round, meta),
    repairedBadgeHtml: ctx.repairedBadge, rightControlsHtml: _rowRightControls(round),
    toolDisplayLabel: td && td.label,
  });
}

/* Command timing is the latest backend progress sample. Reconnect restores
 * that sample; rendering never advances it with a client clock. Older rounds
 * without proven samples keep their ordinary running status and no timer. */
/* Compact duration: 45s / 3m12s / 1h04m. */
function _fmtDur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return m + 'm' + String(rs).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') + 'm';
}

/* Format the latest proven backend sample; no local timer or extrapolation. */
function _cmdTimerState(round) {
  const timing = typeof commandExecutionTiming === 'function'
    ? commandExecutionTiming(round) : null;
  if (!timing) return null;
  const _tf = (typeof t === 'function') ? t : (k, d) => d;
  if (timing.remainingMs !== null) {
    if (timing.remainingMs === 0) {
      return { txt: _tf('toolTimer.terminating', 'terminating…'), cls: ' ptool-cmd-timer-over' };
    }
    return {
      txt: _tf('toolTimer.countdown', '{n} left').replace('{n}', _fmtDur(timing.remainingMs)),
      cls: timing.remainingMs <= 10000 ? ' ptool-cmd-timer-soon' : '',
    };
  }
  return { txt: _fmtDur(timing.elapsedMs), cls: '' };
}

function _renderCmdTimerChip(round) {
  const st = _cmdTimerState(round);
  if (!st) return '';
  const title = t('toolTimer.reported');
  return `<span class="ptool-cmd-timer${st.cls}" title="${escapeHtml(title)}">${escapeHtml(st.txt)}</span>`;
}

/* ── Per-command interrupt button () ──────────────────────────
 * The whole-task Stop button kills the TURN; this kills only the command.
 * The server plants task._cmd_interrupt, the run_command read loop consumes
 * it within ~0.2s, kills the process tree, and the partial output + the
 * interruption marker go back to the model as an ordinary tool result — the
 * turn CONTINUES. Rendered only while the round is searching (a settled
 * round has nothing to interrupt) and only when we can name the task — an
 * interrupt that cannot resolve its taskId is worse than no button. */
function _renderCmdInterruptBtn(round) {
  /* run_command AND code_exec: since  the standalone code_exec
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
    + ` data-tofu-action="_cmdInterruptClick(this,event)">${escapeHtml(_tf('toolCmd.interrupt', 'Interrupt'))}</button>`;
}

/* Explicit handoff leaves the subprocess and its output reader alive. The
 * request names this exact round, so a delayed click cannot detach a later
 * command of the same turn; the server owns the final eligibility decision. */
function _renderCmdBackgroundBtn(round) {
  if (!round || (round.toolName !== 'run_command' && round.toolName !== 'code_exec')) return '';
  const taskId = round._taskId || (typeof _riTaskIdForRound === 'function'
    ? _riTaskIdForRound(round) : '');
  const roundNum = Number(round.roundNum);
  const toolCallId = String(round.toolCallId || '');
  if (!taskId || !Number.isSafeInteger(roundNum) || roundNum < 1 || !toolCallId) return '';
  const _tf = (typeof t === 'function') ? t : (k, d) => d;
  return `<button type="button" class="ptool-cmd-handoff"`
    + ` data-cmd-task="${escapeHtml(String(taskId))}"`
    + ` data-cmd-round="${roundNum}" data-cmd-call="${escapeHtml(toolCallId)}"`
    + ` title="${escapeHtml(_tf('toolCmd.backgroundTip', 'Keep this command running in the background'))}"`
    + ` data-tofu-action="_cmdBackgroundClick(this,event)">${escapeHtml(_tf('toolCmd.background', 'Move to background'))}</button>`;
}

async function _cmdBackgroundClick(btn, ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  if (!btn || btn.disabled) return;
  const taskId = btn.getAttribute('data-cmd-task') || '';
  const roundNum = Number(btn.getAttribute('data-cmd-round'));
  const toolCallId = btn.getAttribute('data-cmd-call') || '';
  if (!taskId || !Number.isSafeInteger(roundNum) || roundNum < 1 || !toolCallId) return;
  const _tf = (typeof t === 'function') ? t : (k, d) => d;
  btn.disabled = true;
  btn.textContent = _tf('toolCmd.backgrounding', 'Moving…');
  let result = null;
  try {
    result = (typeof Api !== 'undefined' && Api.chat)
      ? await Api.chat.backgroundCommand(taskId, roundNum, toolCallId) : null;
  } catch (_e) { result = null; }
  if (result && result.backgrounded === true) return; // round settlement owns the result
  btn.disabled = false;
  btn.textContent = _tf('toolCmd.background', 'Move to background');
  if (typeof showToast === 'function') {
    showToast(_tf(result && result.reason === 'awaiting_stdin'
      ? 'toolCmd.backgroundStdin'
      : result && result.reason === 'capacity'
        ? 'toolCmd.backgroundCapacity' : 'toolCmd.backgroundUnavailable',
      'Cannot move this command to the background right now'));
  }
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
  const runningCommandHtml = renderRunningToolCommandHtml(round, {
    iconHtml: svg,
    rootPillHtml: cmdRootPill,
    timerHtml: _renderCmdTimerChip(round),
    interruptHtml: _renderCmdBackgroundBtn(round) + _renderCmdInterruptBtn(round),
    rightControlsHtml: '',
  }, {
    bodyExpanded: Boolean(round.toolCallId
      && _cmdBodyExpanded.has(_cmdInteractionKey(round))),
    outputExpanded: false,
  });
  if (runningCommandHtml) return runningCommandHtml;

  const runningBrowserHtml = renderToolBrowserExecutionHtml(round, ctx.meta, {
    iconHtml: svg,
    rootPillHtml: cmdRootPill,
    rightControlsHtml: '',
    timerHtml: _renderCmdTimerChip(round),
  }, {
    bodyExpanded: Boolean(round.toolCallId
      && _cmdBodyExpanded.has(_cmdInteractionKey(round))),
    outputExpanded: false,
  });
  if (runningBrowserHtml) return runningBrowserHtml;
  // Web search: show orbit animation
  if (isSearchToolRound(round)) {
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

/* Batch per-item progress pill ().
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
    ? `<span class="ptool-batch-failed" title="${escapeHtml(String(failed))} failed">${escapeHtml(String(failed))} failed</span>`
    : "";
  return `<span class="ptool-batch-progress" title="${escapeHtml(String(done))}/${escapeHtml(String(total))}">${escapeHtml(String(done))}/${escapeHtml(String(total))}${failHtml}</span>`;
}

/* Command interaction state remains lifecycle-owned. The typed presenter
 * receives boolean snapshots and emits only the established data keys/actions. */
/* The typed presenter truncates interaction keys to 512 code units
   (INTERACTION_KEY_UNITS); the lifecycle Sets must hash the same truncated
   value or expand/collapse persistence silently breaks for long toolCallIds. */
function _cmdInteractionKey(round) {
  const raw = round && typeof round.toolCallId === 'string'
    ? round.toolCallId : '';
  return raw.length > 512 ? raw.slice(0, 512) : raw;
}
const _cmdBodyExpanded = new Set();

function _cmdBodyToggle(el, ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  const block = el && el.closest ? el.closest('.ptool-cmd-block') : null;
  if (!block) return;
  const open = !block.classList.contains('cmd-open');
  block.classList.toggle('cmd-open', open);
  if (el.setAttribute) el.setAttribute('aria-expanded', String(open));
  const key = block.getAttribute('data-cmd-key') || '';
  if (key) {
    if (open) _cmdBodyExpanded.add(key);
    else _cmdBodyExpanded.delete(key);
  }
}

/* Done-block output expansion keyed by toolCallId — a user-opened output
 * pane survives per-progress re-renders and timeline syncs, same contract
 * as _cmdBodyExpanded above. */
const _cmdOutputExpanded = new Set();

/* The output fold chip is the only output disclosure — the header row
 * itself is inert, so no nested-control guard is needed here. */
function _cmdOutputOpenToggle(el, ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  const block = el && el.closest ? el.closest('.ptool-cmd-block') : null;
  if (!block || !block.classList.contains('ptool-cmd-hasoutput')) return;
  const open = !block.classList.contains('output-open');
  block.classList.toggle('output-open', open);
  if (el.setAttribute) el.setAttribute('aria-expanded', String(open));
  const key = block.getAttribute('data-output-key') || '';
  if (key) {
    if (open) _cmdOutputExpanded.add(key);
    else _cmdOutputExpanded.delete(key);
  }
}

function _openExternalAsset(button) {
  const url = button && button.dataset ? button.dataset.url : '';
  if (url) window.open(url, '_blank', 'noopener,noreferrer');
}

// Backwards compat alias
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

      _timerPollRecoveryById.set(timerId, Object.freeze({
        polls: Object.freeze(recoveredPolls),
        triggered,
      }));

      if (activeConvId) {
        runtimeScope.requestAuthoritativeConversationRender?.(
          activeConvId, { force: false, forceScroll: false },
        );
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
 * lib/tasks_pkg/tool_dispatch/_pipeline.py). Rounds with the same llmRound were
 * therefore issued IN PARALLEL; rounds with different llmRound are
 * sequential turns. We group contiguous same-llmRound rounds into one
 * `.ptool-turn` container so the UI reflects the real parallelism instead
 * of a flat list.
 *
 * Accuracy guard: we ONLY group on real `llmRound` data. Legacy rounds
 * without it are each their own (solo) group — the old roundNum-gap
 * heuristic is too unreliable to *claim* parallelism visually.
 * Attempt-aware grouping is the pure typed owner in
 * conversation/presentation/tool-execution-groups.ts; this adapter owns HTML. */

/* Typed grouping UI owns batch chrome, rich-slot routing, narration and inspector anchors.
 * Retained ports own individual tool families, Swarm state and task lookup. */
const _toolGroupPorts = {
  text: (...args) => t(...args), icon: (name, size) => Icon(name, size),
  markdown: (content) => renderMarkdown(content),
  cleanTranslation: (content) => typeof stripNoTranslateTags === 'function' ? stripNoTranslateTags(content) : content,
  renderLine: (round, searching) => _renderUnifiedToolLine(round, searching),
  renderSwarm: (round, allRounds) => typeof _buildSwarmPanelHTML === 'function' ? _buildSwarmPanelHTML(round, allRounds) : null,
  renderProgram: (round) => programExecutionCardHtml(round, (key, _fallback, values) => t(key, values)),
  debugEnabled: () => typeof _featureFlags !== 'undefined' && !!_featureFlags.debug_mode,
  taskIdForRound: (round) => typeof _riTaskIdForRound === 'function' ? _riTaskIdForRound(round) : '',
  debugLog: (message) => { if (typeof debugLog === 'function') debugLog(message, 'info'); },
};
const _toolGroupPresentation = createToolGroupPresentation(_toolGroupPorts);
function _createToolGroupRenderer(allRounds) { return _toolGroupPresentation.createSession(allRounds); }
function _renderToolSlot(round, allRounds) { return _toolGroupPresentation.renderSlot(round, allRounds || [round]); }
function _renderDebugEntry(round) { return _toolGroupPresentation.renderInspector(round); }
function _rowRightControls(round) { return `<span class="ptool-row-ctl">${_renderDebugEntry(round)}</span>`; }
function _renderToolGroupsHTML(rounds, allRounds, narration) {
  return _createToolGroupRenderer(allRounds || rounds).renderGroups(rounds, narration);
}

/* Ordered segments own prose placement; full toolRounds remain the authority
 * for rich bodies. Missing inputs fall back to the grouped renderer. */

/* Engine-authored intervention notes (SEG_SYSTEM_NOTE). Inline SVGs per
 * §3.4; the stall lane reuses the chip family's redo glyph so the note and
 * the legacy chip read as the same event. */
const _NOTE_STALL_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M3 2v6h6"/><path d="M3 8a9 9 0 1 0 3-5.7L3 8"/></svg>';
const _NOTE_TODO_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>';
const _NOTE_CONTEXT_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';
const _NOTE_SESSION_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
const _NOTE_MODIFIED_FILES_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';

/* Data-driven noteKind presentation, parity with the typed surface's
 * SYSTEM_NOTE_PRESENTATION (classic-conversation-renderers.ts): every
 * engine-authored injection is its own injection round and must never fall
 * back to a stall label it is not. Unknown kinds keep the generic
 * intervention entry, so a newer backend never breaks an older client. */
const _SEG_NOTE_KIND_PRESENTATION = {
  'intent-stall': { icon: _NOTE_STALL_ICON_SVG, labelKey: 'systemNote.intentStallLabel' },
  'todo-continuation': { icon: _NOTE_TODO_ICON_SVG, labelKey: 'systemNote.todoContinuationLabel' },
  'system-intervention': { icon: _NOTE_STALL_ICON_SVG, labelKey: 'systemNote.interventionLabel' },
  'context-injection': { icon: _NOTE_CONTEXT_ICON_SVG, labelKey: 'systemNote.contextInjectionLabel' },
  'session-context': { icon: _NOTE_SESSION_ICON_SVG, labelKey: 'systemNote.sessionContextLabel' },
};
/* The noteKind alone cannot tell one generic context-injection from another;
 * the durable `reason` (registry id) distinguishes the active-checklist
 * snapshot so it gets its own first-class title + task-list icon instead of
 * hiding behind the card's buried "原因: active_checklist" line. */
const _SEG_NOTE_REASON_PRESENTATION = {
  active_checklist: { icon: _NOTE_TODO_ICON_SVG, labelKey: 'systemNote.activeChecklistLabel' },
  modified_files: { icon: _NOTE_MODIFIED_FILES_ICON_SVG, labelKey: 'systemNote.modifiedFilesLabel' },
};
/* Reasons that specialize a context-injection note into a first-class
 * intervention (own title/icon). These keep their own row and never join the
 * anonymous context-injection group. Parity with the typed surface's
 * SYSTEM_NOTE_REASON_INTERVENTIONS (conversation-view-model.ts). */
const _SEG_NOTE_REASON_INTERVENTIONS = { active_checklist: true, modified_files: true };

/* GENERATED MIRROR of the backend registry's InjectionSpec.presentation —
 * the single source of truth (scripts/gen_system_note_presentation.mjs writes
 * the typed contract; the parity gate scripts/check_note_presentation_parity.mjs
 * fails if this embed drifts). Per-reason title chip shown INSIDE the
 * ptool-text title line (the reason no longer lives in the card head). Each
 * known registry reason gets a translated label and a tone class so its
 * category is scannable at a glance; unknown reasons keep a neutral chip with
 * the raw id so a newer backend never renders blank.
 * BEGIN GENERATED (registry note_presentations) */
const _SEG_NOTE_REASON_CHIP = {
  active_checklist: { labelKey: 'systemNote.reasonActiveChecklist', tone: 'ok' },
  branch_context: { labelKey: 'systemNote.reasonBranch', tone: 'info' },
  evolution_updates: { labelKey: 'systemNote.reasonEvolution', tone: 'info' },
  execution_allowance: { labelKey: 'systemNote.reasonExecutionAllowance', tone: 'info' },
  fallback_synthesis: { labelKey: 'systemNote.reasonFallback', tone: 'info' },
  goal_worker_directive: { labelKey: 'systemNote.reasonGoalDirective', tone: 'info' },
  goal_worker_review: { labelKey: 'systemNote.reasonGoalReview', tone: 'info' },
  intent_stall: { labelKey: 'systemNote.reasonIntentStall', tone: 'warn' },
  intermediate_response: { labelKey: 'systemNote.reasonIntermediate', tone: 'info' },
  lifecycle_recovery: { labelKey: 'systemNote.reasonLifecycle', tone: 'warn' },
  modified_files: { labelKey: 'systemNote.reasonModifiedFiles', tone: 'warn' },
  partial_stream_retry: { labelKey: 'systemNote.reasonStreamRetry', tone: 'info' },
  terminal_recovery: { labelKey: 'systemNote.reasonTerminalRecovery', tone: 'info' },
  todo_incomplete: { labelKey: 'systemNote.reasonTodoIncomplete', tone: 'ok' },
  tool_progress: { labelKey: 'systemNote.reasonToolProgress', tone: 'warn' },
  tool_repeat_reminder: { labelKey: 'systemNote.reasonToolRepeat', tone: 'warn' },
  tool_stagnation: { labelKey: 'systemNote.reasonToolStagnation', tone: 'err' },
  url_prefetch: { labelKey: 'systemNote.reasonUrlPrefetch', tone: 'info' },
  virtual_user_directive: { labelKey: 'systemNote.reasonVirtualUser', tone: 'info' },
  worker_artifacts: { labelKey: 'systemNote.reasonWorkerArtifacts', tone: 'info' },
  worker_dependencies: { labelKey: 'systemNote.reasonWorkerDeps', tone: 'info' },
  worker_wrapup: { labelKey: 'systemNote.reasonWorkerWrapup', tone: 'warn' },
};
/* END GENERATED */
function _segNoteReasonChipHtml(reason) {
  if (!reason) return '';
  const chip = Object.prototype.hasOwnProperty.call(_SEG_NOTE_REASON_CHIP, reason)
    ? _SEG_NOTE_REASON_CHIP[reason] : undefined;
  const label = chip ? t(chip.labelKey) : reason;
  const tone = chip ? chip.tone : 'info';
  return `<span class="sw-note-reason sw-note-reason-${tone}" title="${escapeHtml(t('systemNote.reasonLabel'))}: ${escapeHtml(reason)}">${escapeHtml(label)}</span>`;
}
const _SEG_NOTE_ACTION_BADGE_KEY = {
  prompt: 'systemNote.actionPrompt',
  stop: 'systemNote.actionStop',
  'tool-policy': 'systemNote.actionToolPolicy',
  continue: 'systemNote.actionContinue',
};

/* One engine-authored injection (stall nudge / todo reminder / appended
 * context) as its own timeline block — its own injection round, at the
 * exact wire position. Collapsed by default: visible, not noisy. The body
 * is the verbatim text the model was sent (escaped, never markdown) so the
 * render cannot drift from the wire. */
function _renderSegSystemNoteHTML(s) {
  const noteKind = contextNoteDisplayKind(s);
  const reason = (typeof s.reason === 'string') ? s.reason : '';
  const presentation = (Object.prototype.hasOwnProperty.call(_SEG_NOTE_REASON_PRESENTATION, reason) && _SEG_NOTE_REASON_PRESENTATION[reason])
    || (Object.prototype.hasOwnProperty.call(_SEG_NOTE_KIND_PRESENTATION, noteKind) && _SEG_NOTE_KIND_PRESENTATION[noteKind])
    || _SEG_NOTE_KIND_PRESENTATION['system-intervention'];
  const action = (typeof s.action === 'string') ? s.action : 'prompt';
  const badgeKey = Object.prototype.hasOwnProperty.call(_SEG_NOTE_ACTION_BADGE_KEY, action)
    ? _SEG_NOTE_ACTION_BADGE_KEY[action] : _SEG_NOTE_ACTION_BADGE_KEY.prompt;
  const text = (typeof s.text === 'string') ? s.text : '';
  const reasonChip = _segNoteReasonChipHtml(reason);
  return `<details class="sw-inbox-row sw-system-note-row" data-note-kind="${escapeHtml(noteKind)}"${reason ? ` data-reason="${escapeHtml(reason)}"` : ''}>` +
    `<summary class="ptool-line sw-inbox-row-header">` +
    `<span class="ptool-icon">${presentation.icon}</span>` +
    `<span class="ptool-text">${escapeHtml(t(presentation.labelKey))}</span>` +
    reasonChip +
    `<span class="ptool-badge ptool-badge-info">${escapeHtml(t(badgeKey))}</span>` +
    `</summary>` +
    `<div class="sw-inbox-row-body">` +
    `<div class="sw-card sw-stall-card-item"><div class="sw-card-head"><span class="sw-card-role">${escapeHtml(t('systemNote.detailsLabel'))}</span></div><pre class="sw-card-raw-pre">${escapeHtml(text)}</pre></div>` +
    `</div></details>`;
}
/* One note inside a merged group: a labelled header (its own reason /
 * noteKind title + action badge) above the same verbatim body card the solo
 * row uses. The inner details is purely visual (header row, not a second
 * disclosure), so the group's single disclosure governs expand/collapse. */
function _renderSegSystemNoteItemHTML(s) {
  const noteKind = contextNoteDisplayKind(s);
  const reason = (typeof s.reason === 'string') ? s.reason : '';
  const presentation = (Object.prototype.hasOwnProperty.call(_SEG_NOTE_REASON_PRESENTATION, reason) && _SEG_NOTE_REASON_PRESENTATION[reason])
    || (Object.prototype.hasOwnProperty.call(_SEG_NOTE_KIND_PRESENTATION, noteKind) && _SEG_NOTE_KIND_PRESENTATION[noteKind])
    || _SEG_NOTE_KIND_PRESENTATION['system-intervention'];
  const action = (typeof s.action === 'string') ? s.action : 'prompt';
  const badgeKey = Object.prototype.hasOwnProperty.call(_SEG_NOTE_ACTION_BADGE_KEY, action)
    ? _SEG_NOTE_ACTION_BADGE_KEY[action] : _SEG_NOTE_ACTION_BADGE_KEY.prompt;
  const text = (typeof s.text === 'string') ? s.text : '';
  const reasonChip = _segNoteReasonChipHtml(reason);
  return `<div class="sw-card sw-stall-card-item sw-note-group-item" data-note-kind="${escapeHtml(noteKind)}"${reason ? ` data-reason="${escapeHtml(reason)}"` : ''}>` +
    `<div class="sw-note-group-item-head">` +
    `<span class="ptool-icon">${presentation.icon}</span>` +
    `<span class="ptool-text">${escapeHtml(t(presentation.labelKey))}</span>` +
    reasonChip +
    `<span class="ptool-badge ptool-badge-info">${escapeHtml(t(badgeKey))}</span>` +
    `</div>` +
    `<pre class="sw-card-raw-pre">${escapeHtml(text)}</pre>` +
    `</div>`;
}
/* A maximal run of adjacent context-injection notes (the session-start
 * context stack, or several injected together mid-turn) merged into ONE
 * disclosure row with a per-item list inside. Positions are unchanged — the
 * group still sits at the exact wire slot of its first note; only the count
 * of sibling rows shrinks. */
function _renderSegSystemNoteGroupHTML(notes) {
  const items = notes.map(_renderSegSystemNoteItemHTML).join('');
  return `<details class="sw-inbox-row sw-system-note-row sw-note-group" data-note-group="context-injection">` +
    `<summary class="ptool-line sw-inbox-row-header">` +
    `<span class="ptool-icon">${_NOTE_CONTEXT_ICON_SVG}</span>` +
    `<span class="ptool-text">${escapeHtml(t('systemNote.groupLabel'))}</span>` +
    `<span class="sw-note-group-count">${escapeHtml(t('systemNote.groupItemsTitle', { n: notes.length }))}</span>` +
    `</summary>` +
    `<div class="sw-inbox-row-body sw-note-group-body">${items}</div>` +
    `</details>`;
}
/* Display-only regrouping of one batch's segments: merge each maximal run of
 * >=2 adjacent context-injection notes into a single {_noteGroup} pseudo-
 * segment carrying its members in wire order. Positions, batch keys, and
 * attempt ordinals are untouched — this runs only inside the HTML builder,
 * after computeExecutionBatches has fixed the timeline. */
function _groupSegContextNotes(segs) {
  const out = [];
  let run = null;
  const flush = () => {
    if (!run) return;
    if (run.length > 1) out.push({ _noteGroup: true, notes: run });
    else out.push(run[0]);
    run = null;
  };
  for (const s of segs) {
    if (s && s.type === 'system_note' && s.text
        && isContextInjectionNote(s)) {
      if (run && !sameContextNoteScope(run[0], s)) flush();
      (run = run || []).push(s);
    } else {
      flush();
      out.push(s);
    }
  }
  flush();
  return out;
}
/* Render one llmRound's prose before its resolved rich tool rows. */
function _renderTimelineBatch(batch, rounds, allRounds, idx, renderGroups = _renderToolGroupsHTML) {
  let html = "";
  // Prose first (thinking, then narration text) — the order the model
  // produced it before it called the tools. Non-deliverable text only;
  // the deliverable answer is rendered by the caller AFTER the timeline.
  for (const s of _groupSegContextNotes(batch)) {
    if (s.type === "thinking" && s.text) {
      // Segment-local settled text cannot use msg.thinking's lazy loader.
      // Prefer the stamped per-segment translation (retro/on-open path keys
      // reasoning by blockId) exactly like the narration branch below.
      const _thinkSrc = (s.translatedText && s.translatedText.trim()) ? s.translatedText : s.text;
      const _thinkClean = (typeof stripNoTranslateTags === 'function') ? stripNoTranslateTags(_thinkSrc) : _thinkSrc;
      html += `<details class="thinking-block seg-thinking" data-state="complete"><summary class="thinking-header"><span class="thinking-state-dot" aria-hidden="true"></span><span class="thinking-label">${escapeHtml(t('stream.thinking.done'))}</span><span class="thinking-toggle conversation-disclosure-chevron" aria-hidden="true">${Icon('chevronDown', 16)}</span></summary><div class="thinking-content"><div class="thinking-text thinking-md">${renderMarkdown(_thinkClean)}</div></div></details>`;
    } else if (s.type === "text" && !s.deliverable && s.text) {
      /* Inter-round narration ("Let me check the files.") rendered as its
       * own quiet content block, adjacent to the tools it preceded.
       * When auto-translate committed a per-round Chinese projection onto
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
       * translation pipeline and typed settled-turn renderer do. Without this
       * the settled tool log was the one
       * translated-content site that leaked the raw marker — a clean→dirty
       * snap at finalize. */
      const _segClean = (typeof stripNoTranslateTags === 'function') ? stripNoTranslateTags(_segText) : _segText;
      /* data-seg-round: surgical-update key for the unified per-round translate
       * painter (see _renderSegNarrationHTML). */
      const _rk = (s.llmRound != null) ? ` data-seg-round="L${escapeHtml(String(s.llmRound))}"` : '';
      html += `<div class="md-content seg-narration"${_rk}>${renderMarkdown(_segClean)}</div>`;
    } else if (s._noteGroup) {
      html += _renderSegSystemNoteGroupHTML(s.notes);
    } else if (s.type === "system_note" && s.text) {
      html += _renderSegSystemNoteHTML(s);
    }
  }
  // Then the tool rows for this batch (rich bodies from toolRounds).
  if (rounds.length > 0) {
    html += renderGroups(rounds, allRounds);
  }
  return html;
}

/* Render the interleaved per-tool timeline for a finished message.
 * Returns HTML, or "" when the segment path can't apply (caller falls
 * back to the legacy grouped render). Pure — no DOM mutation. */
function renderSegmentTimelineHTML(segments, msg, idx) {
  return renderToolSegmentTimeline(segments, getToolRoundsFromMsg(msg) || [], {
    groupSegments: (values, hasLlm) => computeExecutionBatches(values, hasLlm),
    isSuperseded: (round) => _isSupersededOrphanRound(round),
    createSession: _createToolGroupRenderer,
    renderBatch: (batch, rounds, allRounds, renderGroups) => _renderTimelineBatch(batch, rounds, allRounds, idx, renderGroups),
    panel: (rounds, allRounds, active) => presentToolExecutionPanel(rounds, allRounds, active, t, escapeHtml, Icon('chevronDown', 16)),
  });
}

function _renderUnifiedGroup(allRounds, segments) {
  return _toolGroupPresentation.renderPanel(_projectTodoRoundsForDisplay(allRounds), segments);
}

document.addEventListener("click", handleToolExecutionDisclosureClick);

/* Disclosure rows may contain a nested source/copy control, so the row cannot
 * itself be a native <button>. Give the ARIA button the same Enter/Space
 * contract without stealing keyboard events from those nested controls. */
document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter" && e.key !== " ") return;
  const target = e.target;
  if (!target || typeof target.closest !== "function") return;
  const disclosure = target.closest(
    '.ptool-results-header[role="button"],.timer-watcher-header[role="button"]',
  );
  if (!disclosure || target !== disclosure) return;
  e.preventDefault();
  disclosure.click();
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

// Timer-id chip → copy the full timer id to the clipboard. Delegated at the
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

// Backwards compat aliases
const _renderProjectGroup = _renderUnifiedGroup;
const _renderBrowserGroup = _renderUnifiedGroup;

/* The shared predicate keys on authoritative superseded/result-less metadata,
 * independently of live/reloaded status. Genuine user interruptions stay visible. */
function _isSupersededOrphanRound(r) { return isSupersededToolOrphan(r); }

function renderToolRoundsHTML(rounds, isStreaming, segments) {
  if (!rounds || rounds.length === 0) return "";
  rounds = rounds.filter((r) => !(r && r.toolName === "execute_tools"));
  if (rounds.length === 0) return "";
  /* Drop superseded orphan rounds (FloorRetry/stream-retry duplicates left
   *   result-less and reconciled) so they never render a misleading
   *   "interrupted" chip. The user's real call is the adopted/recovered twin.
   *   Genuine user-Stop interruptions (badge='interrupted') are kept. */
  rounds = rounds.filter((r) => !_isSupersededOrphanRound(r));
  if (rounds.length === 0) return "";
  /* UNIFIED: every round — tool calls AND swarm panels — goes into
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
  /* Overleaf live preview: a newly settled mcp__overleaf__ edit round in a
   * live stream recompiles the remote paper and scrolls the side panel to
   * the edit. Pure scan; the lazy chunk loads only on a real candidate. */
  return _renderUnifiedGroup(rounds, segments);
}

function _toolElapsedDocumentHidden() {
  return typeof document !== 'undefined'
    && (document.hidden === true || document.visibilityState === 'hidden'
      || runtimeScope.nativeVisibility?.isHidden() === true);
}

function _subscribeToolElapsedVisibility(listener) {
  if (typeof document === 'undefined'
      || typeof document.addEventListener !== 'function') return () => {};
  document.addEventListener('visibilitychange', listener);
  return () => document.removeEventListener?.('visibilitychange', listener);
}

function _tickToolElapsedTimers() {
  const watcherTimerActive = typeof _tickTimerCountdowns === 'function'
    ? _tickTimerCountdowns() : false;
  return watcherTimerActive;
}

const ToolElapsedTicker = createDemandScopedPresentationTicker({
  schedule: {
    setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
    clearTimeout: (handle) => globalThis.clearTimeout(handle),
  },
  visibility: {
    isHidden: _toolElapsedDocumentHidden,
    subscribe: _subscribeToolElapsedVisibility,
  },
  tick: _tickToolElapsedTimers,
  onError: (error) => console.warn('[ToolTimers] tick failed: ' +
    (error?.message || error)),
});

function _demandToolElapsedTicker() {
  if (typeof window !== 'undefined') ToolElapsedTicker.demand();
}

/* ── Lazy thinking expand ────────────────────────────────
   Don't dump 30-100k+ chars of thinking text into the DOM
   on every render — inject it only when the user expands.
   This prevents DevTools / Elements tab from choking.      */
