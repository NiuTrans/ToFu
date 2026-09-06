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
  return `<span class="ptool-badge ${cls}" title="${tip}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/></svg> ${label}</span>`;
}

/* ── Compacted-result read-back chip ────────────────────────────────────
 * read_tool_artifact / search_tool_artifact continue a PRIOR round's spilled
 * result. The flat server label stacks two verbs
 * ("Read compacted result of R54 · Read 1 file: panel.ts"), which reads as a
 * plain read_files row. Re-compose the title as an origin chip + the source
 * call's own label: the chip carries the read-back action and the source
 * round anchor, the text after it stays the original call's display.
 * Structured `_artifactOrigin` (attached at round-build time, like
 * `_toolRoot`) is the authority; the regex fallback covers recovery-rebuilt
 * rounds and history persisted before the meta existed — those carry only
 * the flat query string. */
const _ARTIFACT_LABEL_RE =
  /^(Read|Search) compacted result(?: of R(\d+))?(?: · ([\s\S]*))?$/;

function _artifactContinuationParts(round) {
  if (!round || (round.toolName !== "read_tool_artifact"
      && round.toolName !== "search_tool_artifact")) return null;
  const origin = round._artifactOrigin;
  if (origin && typeof origin === "object") {
    const listed = Array.isArray(origin.queries) && origin.queries.length
      ? origin.queries : [origin.query];
    return {
      kind: origin.kind === "search" ? "search" : "read",
      sourceRound: Number.isInteger(origin.sourceRound) ? origin.sourceRound : null,
      source: String(origin.source || ""),
      queries: listed.map((q) => String(q || "").trim()).filter(Boolean),
    };
  }
  const m = _ARTIFACT_LABEL_RE.exec(String(round.query || ""));
  if (!m) return null;
  return {
    kind: m[1] === "Search" ? "search" : "read",
    sourceRound: m[2] ? parseInt(m[2], 10) : null,
    source: m[3] || "",
    // Legacy flat labels mingle the pattern into the source tail
    // ("web_search: citadel: download url") — already visible, no chips.
    queries: [],
  };
}

function _artifactContinuationChip(parts) {
  const isSearch = parts.kind === "search";
  const icon = isSearch
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>';
  const from = parts.sourceRound ? `R${parts.sourceRound}` : "an earlier round";
  const tip = isSearch
    ? `Searches inside the compacted tool result spilled from ${from}`
    : `Reads back a slice of the compacted tool result spilled from ${from}`;
  const roundLabel = parts.sourceRound ? `R${parts.sourceRound} ` : "";
  return `<span class="ptool-badge ptool-badge-artifact" title="${escapeHtml(tip)}">${icon}${escapeHtml(roundLabel)}compacted</span>`;
}

function _artifactContinuationTitle(round, parts) {
  const src = parts.source
    ? _linkifyMcpLabels(escapeHtml(parts.source).replace(/\n/g, '<br>'), round)
    : "";
  let html = _artifactContinuationChip(parts) + (src ? ` ${src}` : "");
  // The actual patterns being searched inside the spill — without them the
  // row answered WHERE (origin chip + source label) but never WHAT.
  const queries = Array.isArray(parts.queries) ? parts.queries : [];
  if (queries.length) {
    const chips = queries.slice(0, 3).map((q) =>
      `<code class="ptool-artifact-query">${escapeHtml(q)}</code>`);
    const rest = queries.slice(3);
    if (rest.length) {
      chips.push(`<code class="ptool-artifact-query ptool-artifact-query-more" title="${escapeHtml(rest.join("\n"))}">+${rest.length}</code>`);
    }
    html += ` ${chips.join(" ")}`;
  }
  return html;
}

function _toolRejectionDescriptor(round) {
  const meta = (round && Array.isArray(round.results) ? round.results : [])[0] || {};
  return (round && (round.rejection || round._rejected))
    || meta.rejection || meta.rejected || meta._rejected || {};
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
  const firstLine = rawReason.split("\n")[0].trim();
  const shortReason = firstLine.length > 300 ? firstLine.slice(0, 300) + "…" : firstLine;
  const tip = escapeHtml(rawReason || fallbackTip);
  let suggHtml = "";
  if (isHallucinated && sugg.length) {
    const chips = sugg.map((s) => `<code class="ptool-reject-sugg">${escapeHtml(s)}</code>`).join(" ");
    const did = escapeHtml(_t("tool.didYouMean", "did you mean"));
    suggHtml = `<span class="ptool-reject-hint">${did} ${chips}?</span>`;
  }
  /* SVG glyph (§3.4 — no emoji): a circle-slash "forbidden" mark. */
  const banSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><circle cx="12" cy="12" r="9"/><line x1="5.6" y1="5.6" x2="18.4" y2="18.4"/></svg>`;
  return `<div class="ptool-line ptool-rejected" title="${tip}">
       <span class="ptool-icon">${svg}</span>
       <span class="ptool-text${isHallucinated ? " ptool-reject-name" : ""}">${escapeHtml(attempted)}</span>
       <span class="ptool-badge ptool-badge-reject">${banSvg} ${badgeLabel}</span>
       ${suggHtml}
       ${shortReason ? `<span class="ptool-error-reason">${escapeHtml(shortReason)}</span>` : ""}
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

/* Checklist block (todo_write) — a collapsible progress card rendered off
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
function _projectTodoRoundsForDisplay(rounds) {
  const source = Array.isArray(rounds) ? rounds : [];
  const todoIdx = [];
  source.forEach((r, i) => {
    if (r && r.toolName === "todo_write") todoIdx.push(i);
  });
  if (todoIdx.length <= 1) return source;
  const keep = todoIdx[todoIdx.length - 1];
  const todoSet = new Set(todoIdx);
  /* A rejected/no-op call is protocol feedback for the model, not a new
   * checklist revision for the person reading the conversation. Keep those
   * receipts in Turn authority/debug history while deriving the visible
   * revision history solely from accepted state transitions. */
  const seenAcceptedRevisions = new Set();
  const history = todoIdx.flatMap((idx) => {
    const row = source[idx] || {};
    const result = (row.results || [])[0] || {};
    if (result.todoRejected || result.todoNoop) return [];
    const items = Array.isArray(result.todos) ? result.todos : [];
    const revision = Number(result.todoRevision || 0);
    const identity = revision > 0
      ? `${result.checklistId || "checklist"}:${revision}`
      : `round:${row.roundNum ?? idx}`;
    if (seenAcceptedRevisions.has(identity)) return [];
    seenAcceptedRevisions.add(identity);
    return [{
      roundNum: row.roundNum,
      revision: revision || result.todoRevision,
      operation: result.todoOperation || "sync",
      done: items.filter((x) => x && x.status === "completed").length,
      total: items.length,
    }];
  });
  return source.reduce((out, r, i) => {
    if (todoSet.has(i) && i !== keep) return out;
    if (i !== keep) {
      out.push(r);
      return out;
    }
    const latestResult = (r.results || [])[0] || {};
    const acceptedUpdateCount = Number(latestResult.todoUpdateCount || 0)
      || history.length || 1;
    const projected = { ...r, todoDisplayUpdateCount: acceptedUpdateCount };
    if (Array.isArray(r.results) && r.results.length) {
      projected.results = r.results.slice();
      if (r.results[0] && typeof r.results[0] === "object") {
        projected.results[0] = {
          ...r.results[0],
          todoDisplayUpdateCount: acceptedUpdateCount,
          todoDisplayHistory: history,
        };
      }
    }
    out.push(projected);
    return out;
  }, []);
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
  // Protocol-only adapter: its real children render as ordinary tool rows.
  if (round && round.toolName === "execute_tools") return "";
  const svg = toolRoundSvg(round);
  const td = _getToolDisplay(round);
  /* Preserve real newlines in the tool-call title — batch search/fetch
   * displays render one item per line so users can see every candidate
   * without elision. escapeHtml first (HTML-safe), THEN substitute
   * \n → <br> so the browser actually breaks the line. */
  const artifactParts = _artifactContinuationParts(round);
  const q = artifactParts
    ? _artifactContinuationTitle(round, artifactParts)
    : (round.query
      ? _linkifyMcpLabels(escapeHtml(round.query).replace(/\n/g, '<br>'), round)
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
  if (((recoveredTimer.polls && recoveredTimer.polls.length > 0) || round._timerSkipCount)
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
    rootPillHtml: rootPill,
    rightControlsHtml: _rowRightControls(round),
  });
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

  // Determine badge
  const badgeHtml = _computeToolBadgeHtml(round, ctx);

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
  const cmdText = escapeHtml(round.query || meta.title || td.label || round.toolName || "");
  return `<div class="ptool-line ptool-interrupted" data-rn="${round.roundNum}">
         <span class="ptool-icon">${svg}</span>
         ${rootPill}
         <span class="ptool-text">${cmdText}</span>
         <span class="ptool-badge ptool-badge-interrupted">interrupted</span>
       </div>`;
}

/* Terminal tool errors show the first model-visible reason inline, with no
 * spinner or success badge. */
function _renderErrorRow(round, ctx) {
  const { svg, td, meta, rootPill } = ctx;
  if (round.status !== "error") return "";
  const _t = (typeof t === "function") ? t : (k, d) => d;
  /* Same linkify as the done lane: a failed read_doc still renders its doc
   * title as a clickable link — failure must not strip navigation. */
  const cmdText = _linkifyMcpLabels(
    escapeHtml(round.query || meta.title || td.label || round.toolName || "").replace(/\n/g, "<br>"),
    round);
  const reason = (typeof round.toolContent === "string" ? round.toolContent : "")
    .split("\n")[0].trim();
  const short = escapeHtml(reason.length > 300 ? reason.slice(0, 300) + "…" : reason);
  const tip = escapeHtml(reason || _t("tool.failedTip", "The tool failed — it did not complete successfully."));
  return `<div class="ptool-line ptool-error" data-rn="${round.roundNum}" title="${tip}">
         <span class="ptool-icon">${svg}</span>
         ${rootPill}
         <span class="ptool-text">${cmdText}</span>
         <span class="ptool-badge ptool-badge-err">${escapeHtml(_t("tool.failed", "failed"))}</span>
         ${short ? `<span class="ptool-error-reason">${short}</span>` : ""}
       </div>`;
}

/* ── Live run_command timer () ────────────────────────────────
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
  _demandToolElapsedTicker();
  const anchor = _cmdTimerAnchor(round);
  const dl = (typeof round.deadlineTs === 'number' && round.deadlineTs > 0) ? round.deadlineTs : '';
  /* The data-* attributes are what the ticker updates in place — no re-render,
   * so the fingerprint gate in _syncToolRoundsDOM (which correctly skips when
   * no SSE event landed) cannot freeze the value. */
  return `<span class="ptool-cmd-timer${st.cls}" data-cmd-timer="1"`
    + ` data-cmd-anchor="${anchor == null ? '' : anchor}"`
    + ` data-cmd-deadline="${dl}">${escapeHtml(st.txt)}</span>`;
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
    interruptHtml: _renderCmdInterruptBtn(round),
    rightControlsHtml: '',
  }, {
    bodyExpanded: Boolean(round.toolCallId
      && _cmdBodyExpanded.has(_cmdInteractionKey(round))),
    outputExpanded: false,
  });
  if (runningCommandHtml) return runningCommandHtml;
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

/* The command header toggles output only from its own non-control space. */
function _cmdHeaderToggle(el, ev) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  const block = el && el.closest ? el.closest('.ptool-cmd-block') : null;
  if (!block || !block.classList.contains('ptool-cmd-hasoutput')) return;
  const target = ev && ev.target;
  if (target && typeof target.closest === 'function') {
    const nestedAction = target.closest('[data-tofu-action]');
    if (nestedAction && nestedAction !== el) return;
    if (target.closest('button, a, input, select, textarea')) return;
  }
  const open = !block.classList.contains('output-open');
  block.classList.toggle('output-open', open);
  if (el.setAttribute) el.setAttribute('aria-expanded', String(open));
  const key = block.getAttribute('data-output-key') || '';
  if (key) {
    if (open) _cmdOutputExpanded.add(key);
    else _cmdOutputExpanded.delete(key);
  }
}
/* Named action required by the restricted data-tofu-action interpreter. */
function _cmdOutputToggle(el, ev, kind) {
  if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
  const wrap = el && el.parentElement;
  if (!wrap || !wrap.classList) return;
  const tf = (typeof t === 'function') ? t : (k, d) => d;
  const open = wrap.classList.toggle('expanded');
  const label = open
    ? tf('toolCmd.collapse', 'Collapse')
    : tf(kind === 'result' ? 'toolCmd.showResult' : 'toolCmd.showOutput', 'Show output');
  el.innerHTML = `${open ? _PT_CHEVRON_DOWN_SVG : _PT_CHEVRON_RIGHT_SVG}${escapeHtml(label)}`;
}

function _openExternalAsset(button) {
  const url = button && button.dataset ? button.dataset.url : '';
  if (url) window.open(url, '_blank', 'noopener,noreferrer');
}

// Determine the trailing badge (explicit meta.badge → token count → fetched
// chars → generic ✓ done).
function _computeToolBadgeHtml(round, ctx) {
  const { meta, results } = ctx;
  let badgeHtml = "";
  if (meta.badge) {
    const refusal = _refusalInfo(round, meta);
    if (refusal) {
      badgeHtml = _renderGateRefusalBadgeHtml(refusal);
    } else {
    const isWrite =
      round.toolName === "write_file" || round.toolName === "edit_file" ||
      round.toolName === "apply_diff" || round.toolName === "apply_diffs" ||
      round.toolName === "insert_content" ||
      round.toolName === "insert_contents";
    const ok = meta.writeOk !== false;
    /* A successful memory op (meta.memoryOk, set by the backend memory
     * handler) reads as a "save" — show the solid green OK badge, same as
     * a write tool, instead of the neutral yellow info badge. */
    const isMemoryOk = meta.memoryOk === true;
    /* await_agents timeout: amber warning badge so a partial result
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
    /* Per-tool token count — emitted by lib/tasks_pkg/tool_dispatch/_pipeline.py
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
  /* Compaction badge — flag tool calls whose content has been replaced
   * with a placeholder (L0 = budget/persist to disk, L1 = micro_compact
   * cold-tail). The model now sees only a short marker; clicking the
   * preview button still opens the original toolContent. */
  /* The old per-row "🗜 L1 280k→2k" badge previously appended to
   * badgeHtml was REMOVED — the inline COMPACTED L1 pill rendered
   * before the tool name (see compactionLabelHtml below) carries the
   * same information at higher visibility, and showing both clutters
   * the row. */
  // Generic tool done with no results and no badge — show a plain status.
  if (!badgeHtml && !isProjectToolRound(round) && !isBrowserToolRound(round) && results.length === 0) {
    const elapsed = round._elapsed ? ` · ${round._elapsed}` : "";
    badgeHtml = `<span class="ptool-badge ptool-badge-ok">done${elapsed}</span>`;
  }
  return badgeHtml;
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

/* git-fork glyph (two parents → one child) — reads as "these calls
 * branched off the same turn". SVG only, per CLAUDE.md §3.4. */
const _turnForkSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/></svg>';

function _turnLabelText(size) {
  return t("toolPanel.parallelCalls", { n: size });
}

/* Standalone round tag for a SOLO turn (one tool call). Solo groups are
 * display:contents (no box), so this renders as a thin label line above
 * the single tool row. */
function _renderSoloRoundTag(group) {
  return `<div class="ptool-turn-rno ptool-turn-rno-solo" title="${escapeHtml(toolGroupRoundTitle(group, t))}">${escapeHtml(toolGroupRoundDisplay(group))}</div>`;
}

/* The collapsible header shown atop a multi-call .ptool-turn container.
 * Shared verbatim by the static renderer and the streaming sync path so
 * a finalize swap is seamless. `rno` (optional) prefixes the round number
 * so the header reads e.g. `第3轮 · 2 parallel calls`. */
function _renderTurnHead(size, group, collapsed) {
  const rno = toolGroupRoundNumber(group);
  const rnoHtml = rno != null ? `<span class="ptool-turn-rno" title="${escapeHtml(toolGroupRoundTitle(group, t))}">${escapeHtml(toolGroupRoundDisplay(group))}</span>` : "";
  return `<button type="button" class="ptool-turn-head" aria-expanded="${String(!collapsed)}">${rnoHtml}${_turnForkSvg}<span class="ptool-turn-label">${_turnLabelText(size)}</span><span class="ptool-turn-chev">${Icon('chevronDown', 16)}</span></button>`;
}

function _programFlowLabel(r) {
  const base = t("ptc.flow.orchestrated");
  if (r && r.programBackend === "local_toolscript") {
    return `${base} · ToolScript (local)`;
  }
  if (r && r.programBackend === "native_openai") {
    return `${base} · OpenAI (native)`;
  }
  return base;
}

function _renderProgramRound(r) {
  const running = r.status === "searching" || r.programStatus === "running";
  const status = running ? t("ptc.status.running")
    : (r.programStatus === "completed" ? t("ptc.status.completed")
      : (r.programStatus === "error" ? t("ptc.status.failed")
        : t("ptc.status.incomplete")));
  const tools = Array.from(new Set((r.childToolNames || []).filter(Boolean)));
  const count = (r.childCallIds || []).length || tools.length;
  const maxCalls = r.programLimits && r.programLimits.maxCalls;
  const maxConcurrent = r.programLimits && r.programLimits.maxConcurrentCalls;
  const limits = [];
  if (maxCalls) limits.push(t("ptc.limit.calls", { n: maxCalls }));
  if (maxConcurrent) limits.push(t("ptc.limit.concurrent", { n: maxConcurrent }));
  const limitText = limits.length ? ` · ${limits.join(" · ")}` : "";
  const toolChips = tools.map((name) =>
    `<span class="ptc-tool-chip">${escapeHtml(name)}</span>`).join("");
  const code = String(r.programCode || "");
  const result = programDisplayValue(r.programResult);
  const codeBlock = code
    ? `<details class="ptc-detail"><summary>${escapeHtml(t("ptc.code"))}</summary><pre><code>${escapeHtml(code)}</code></pre></details>`
    : "";
  const resultBlock = result
    ? `<details class="ptc-detail ptc-result"><summary>${escapeHtml(t("ptc.result"))}</summary><pre>${escapeHtml(result)}</pre></details>`
    : "";
  const icon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m8 9-3 3 3 3"/><path d="m16 9 3 3-3 3"/><path d="m14 5-4 14"/></svg>';
  return `<section class="ptc-card${running ? " ptc-running" : ""}">` +
    `<div class="ptc-head"><span class="ptc-icon">${icon}</span>` +
    `<span class="ptc-title">${escapeHtml(t("ptc.title"))}</span>` +
    `<span class="ptc-status">${escapeHtml(status)}</span></div>` +
    `<div class="ptc-meta">${escapeHtml(_programFlowLabel(r))} · ${escapeHtml(t("ptc.tasks", { n: count }))}${escapeHtml(limitText)}</div>` +
    (toolChips ? `<div class="ptc-tools">${toolChips}</div>` : "") +
    codeBlock + resultBlock + `</section>`;
}

/* Render one tool round into its `[data-prn]` slot. Swarm rounds get the
 * full agent dashboard; everything else the compact tool line. `allRounds`
 * is the full timeline (swarm panels need it for cross-round context).
 *
 * The debug entry rides inside the row's own header (see _rowRightControls),
 * occupying its own space in the row's flex flow instead of floating over
 * anything. Swarm panels have no `.ptool-line` header of that shape, so they
 * — and only they — still get a standalone entry appended after the panel. */
/* Sibling-title collision guard. The backend composes each round's query
 * per call, unaware of its siblings; a parallel batch can therefore contain
 * several calls whose composed titles collide (same tool, same resource,
 * differing only in args the title elided). siblingTitleDiscriminators
 * derives a ` · key=value` suffix from the args that ACTUALLY differ across
 * the colliding cluster — or ` #n` when even the args are byte-equal — so no
 * two rows in one batch ever look like the same execution repeated. Keyed
 * by durable toolCallId; memoized per timeline array so a full-turn render
 * computes it once per pass, not once per row. */
const _discrimCache = new WeakMap();
function _siblingTitleSuffix(r, allRounds) {
  if (!Array.isArray(allRounds) || typeof siblingTitleDiscriminators !== 'function') return '';
  if (!r || r.toolCallId == null) return '';
  let m = _discrimCache.get(allRounds);
  if (!m) {
    m = siblingTitleDiscriminators(allRounds);
    _discrimCache.set(allRounds, m);
  }
  return m.get(String(r.toolCallId)) || '';
}

function _renderToolSlot(r, allRounds) {
  if (isProgramToolRound(r)) {
    return `<div data-prn="${r.roundNum}" data-prn-kind="program">${_renderProgramRound(r)}</div>`;
  }
  const suffix = _siblingTitleSuffix(r, allRounds);
  const round = suffix ? { ...r, query: String(r.query || '') + suffix } : r;
  const isSwarm = isSwarmToolRound(round);
  const inner = (isSwarm && typeof _buildSwarmPanelHTML === 'function')
    ? _buildSwarmPanelHTML(round, allRounds)
    : _renderUnifiedToolLine(round, round.status === "searching");  // panel DEFERRED: generic line (Epic-E sub-5B)
  const swarmAttr = isSwarm ? ' data-prn-kind="swarm"' : '';
  const parentId = toolParentCallId(round);
  const parentAttr = parentId
    ? ` class="ptool-parent-child" data-parent-tool-call-id="${escapeHtml(parentId)}"`
    : '';
  const trailing = isSwarm ? _renderStandaloneDebugEntry(round) : '';
  return `<div data-prn="${round.roundNum}"${swarmAttr}${parentAttr}>${inner}${trailing}</div>`;
}

/* Debug anchors are per tool row, not per bubble. llmRound is zero-based, so
 * request R(llmRound+1) produced the call and R(llmRound+2) consumed its
 * result. The post-tool mirror supersedes the former duplicate request/result
 * controls; data-ri-state links the row to that state. Only render when both
 * task and round identity are known.
 * Swarm rounds are the exception: the row carries `agentId`, its llmRound is
 * the sub-agent's 1-based loop round, and the snapshots live under the
 * agent's OWN inspector stream `{parent}#agent:{agentId}` (lib/swarm/
 * agent.py persists kind='request' rows only, so the panel degrades to the
 * request axis there). */
function _renderDebugEntry(r) {
  if (typeof _featureFlags === 'undefined' || !_featureFlags.debug_mode) return '';
  if (!r || r._inboxInject || r._peerInject || r._userSteerInject || r._bgCommandInject || r._stallNudge || r._programSynthetic) return '';
  const baseTaskId = r._taskId || (typeof _riTaskIdForRound === 'function'
    ? _riTaskIdForRound(r) : '');
  const lr = r.llmRound;
  if (!baseTaskId || lr == null) return '';
  const agentId = r.agentId ? String(r.agentId) : '';
  const taskId = agentId ? `${baseTaskId}#agent:${agentId}` : baseTaskId;
  const round = agentId ? Number(lr) : Number(lr) + 1;  // chat llmRound 0-based → roundNum 1-based
  const tip = (typeof t === 'function') ? t('ri.toolAnchorTip', { round }) : '';
  const esc = escapeHtml(String(taskId));
  return `<button type="button" class="ri-tool-anchor" ` +
    `data-ri-state="${esc}:${round}" ` +
    `title="${escapeHtml(tip)}" ` +
    `data-tofu-action="openToolDebugPanel('${esc}',${round},this)">` +
    `${_RI_TOOL_ANCHOR_SVG}<span class="ri-tool-anchor-label">R${round}</span></button>`;
}

/* Swarm-panel variant: the dashboard has no shared header to sit in, so the
 * entry gets its own right-aligned strip UNDER the panel. Still a real block
 * in normal flow — never a negative-margin overlay. */
function _renderStandaloneDebugEntry(r) {
  const btn = _renderDebugEntry(r);
  return btn ? `<div class="ri-tool-anchor-row">${btn}</div>` : '';
}

/* Single owner of the row's right edge; currently contains only debug entry. */
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
 * Keyed "L<llmRound>" to match computeToolBatches' batch key so the grouped
 * panel can render each round's narration adjacent to that round's tools. */
function _narrationByRound(segments) {
  const m = new Map();
  if (!Array.isArray(segments)) return m;
  const hasLlm = segments.some((segment) => toolExecutionLlmRound(segment) != null);
  for (const group of computeExecutionBatches(segments, hasLlm)) {
    for (const s of group.items) {
      /* Tool/thinking segments participate in occurrence tracking so a
       * legacy L0,L1,L0 resume sequence aligns with tool-round batches. */
      if (!s || s.type !== "text" || s.deliverable) continue;
      const en = s.text || "";
      const zh = s.translatedText || "";
      if (!en.trim() && !zh.trim()) continue;
      if (!m.has(group.key)) m.set(group.key, []);
      m.get(group.key).push(s);
    }
  }
  return m;
}

/* Narration uses the same classes as live and settled timelines; translated
 * text wins per segment and otherwise falls back to source narration. */
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
  const localGroups = computeToolBatches(rounds);
  const contextGroups = ctx === rounds ? localGroups : computeToolBatches(ctx);
  return localGroups.map((g) => {
    const first = g.rounds[0];
    const firstId = first && first.toolCallId ? String(first.toolCallId) : "";
    const contextGroup = contextGroups.find((candidate) => (
      candidate.rounds.includes(first)
      || (firstId && candidate.rounds.some((row) => (
        row && String(row.toolCallId || "") === firstId
      )))
    )) || g;
    const slots = g.rounds.map((r) => _renderToolSlot(r, ctx)).join("");
    const programs = g.rounds.filter(isProgramToolRound);
    const size = g.rounds.length - programs.length;
    const collapsed = !programs.length && shouldCollapseToolBatch(g.rounds);
    const rno = toolGroupRoundNumber(contextGroup);
    const head = programs.length ? ""
      : (size >= 2 ? _renderTurnHead(size, contextGroup, collapsed)
        : (rno != null ? _renderSoloRoundTag(contextGroup) : ""));
    const narr = (narrByRound && narrByRound.get) ? _renderSegNarrationHTML(narrByRound.get(g.key)) : "";
    return narr + `<div class="ptool-turn${programs.length ? " ptool-program-turn" : ""}${collapsed ? " collapsed" : ""}"${size >= 2 ? ' data-collapsible="true"' : ''} data-attention="${summarizeToolAttention(g.rounds).dominant}" data-llm-round="${escapeHtml(String(g.key))}" data-batch-size="${size}" data-round-no="${rno != null ? rno : ""}" data-attempt-ordinal="${contextGroup.attemptOrdinal || ""}">${head}${slots}</div>`;
  }).join("");
}

/* Ordered segments own prose placement; full toolRounds remain the authority
 * for rich bodies. Missing inputs fall back to the grouped renderer. */

/* Build ordered tool-call-id queues (fallback to positional order for legacy
 * rounds that predate stable toolCallId stamping). Provider call ids can repeat
 * within one Turn, so a scalar Map would silently bind every segment to either
 * the first or last crop/result instead of its own occurrence. */
function _roundsByToolCallId(rounds) {
  const byId = new Map();
  const noId = [];
  for (const r of (rounds || [])) {
    if (r && r.toolCallId) {
      const callId = String(r.toolCallId);
      if (!byId.has(callId)) byId.set(callId, []);
      byId.get(callId).push(r);
    } else noId.push(r);
  }
  return { byId, noId };
}

/* Engine-authored intervention notes (SEG_SYSTEM_NOTE). Inline SVGs per
 * §3.4; the stall lane reuses the chip family's redo glyph so the note and
 * the legacy chip read as the same event. */
const _NOTE_STALL_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M3 2v6h6"/><path d="M3 8a9 9 0 1 0 3-5.7L3 8"/></svg>';
const _NOTE_TODO_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>';

/* One engine-authored intervention (stall nudge / todo reminder) as its own
 * timeline block, in the injection-chip visual family but keyed off the
 * SEGMENT — it sits at the exact wire position instead of the chip's
 * recomputed anchor. Collapsed by default: visible, not noisy. The body is
 * the verbatim text the model was sent (escaped, never markdown) so the
 * render cannot drift from the wire. */
function _renderSegSystemNoteHTML(s) {
  const kind = (s.noteKind === 'todo-continuation') ? 'todo-continuation' : 'intent-stall';
  const labelKey = (kind === 'todo-continuation')
    ? 'systemNote.todoContinuationLabel' : 'systemNote.intentStallLabel';
  const icon = (kind === 'todo-continuation') ? _NOTE_TODO_ICON_SVG : _NOTE_STALL_ICON_SVG;
  const text = (typeof s.text === 'string') ? s.text : '';
  return `<details class="sw-inbox-row sw-system-note-row" data-note-kind="${kind}">` +
    `<summary class="ptool-line sw-inbox-row-header">` +
    `<span class="ptool-icon">${icon}</span>` +
    `<span class="ptool-text">${escapeHtml(t(labelKey))}</span>` +
    `<span class="ptool-badge ptool-badge-info">${escapeHtml(t('peer.injectRowBadge'))}</span>` +
    `</summary>` +
    `<div class="sw-inbox-row-body">` +
    `<div class="sw-card sw-stall-card-item"><div class="sw-card-head"><span class="sw-card-role">${escapeHtml(t('stall.promptLabel'))}</span></div><pre class="sw-card-raw-pre">${escapeHtml(text)}</pre></div>` +
    `</div></details>`;
}
/* Render one llmRound's prose before its resolved rich tool rows. */
function _renderTimelineBatch(batch, rounds, allRounds, idx) {
  let html = "";
  // Prose first (thinking, then narration text) — the order the model
  // produced it before it called the tools. Non-deliverable text only;
  // the deliverable answer is rendered by the caller AFTER the timeline.
  for (const s of batch) {
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
    } else if (s.type === "system_note" && s.text) {
      html += _renderSegSystemNoteHTML(s);
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
  const rawRounds = getToolRoundsFromMsg(msg) || [];
  const allRounds = _projectTodoRoundsForDisplay(rawRounds);
  const _projectedIds = new Set(allRounds.filter(Boolean).map((r) => r.toolCallId).filter(Boolean).map(String));
  const _collapsedTodoTcIds = new Set(rawRounds.filter((r) => r && r.toolName === "todo_write" && r.toolCallId
    && !_projectedIds.has(String(r.toolCallId))).map((r) => String(r.toolCallId)));
  if (rawRounds.filter((r) => r && r.toolName === "todo_write" && !r.toolCallId).length > 1) return "";
  // Program parents are intentionally absent from segments (display-only).
  // Let the grouped renderer own this turn so the parent cannot be lost or
  // mistaken for a positional tool body.
  if (allRounds.some((r) => !!(r && r._programSynthetic))) return "";
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
  /* The stall nudge is dual-recorded: the display-only sidecar chip AND a
   * system_note segment at its wire position. When the segment is present it
   * owns the render — skip the chip or the same intervention shows twice.
   * The chip stays the only render for turns that predate note recording. */
  const _hasStallNote = segments.some((segment) => (
    segment && segment.type === "system_note" && segment.noteKind === "intent-stall"
  ));
  const _injByAnchor = new Map();
  const realRounds = [];
  /* Drop backend-stamped result-less superseded rounds before segment
   * resolution and header counts; retain their call IDs so matching tool_use
   * segments cannot fall through to positional resolution. */
  const _supersededTcIds = new Set();
  for (const r of allRounds) {
    if (r && (r._userSteerInject || r._peerInject || r._inboxInject || r._bgCommandInject || r._stallNudge)) {
      if (r._stallNudge && _hasStallNote) continue;
      const injRound = r._userSteerInject ? r.steerRound
        : (r._peerInject ? r.peerRound
          : (r._bgCommandInject ? r.bgCommandRound
            : (r._stallNudge ? r.stallRound : r.inboxRound)));
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

  const byIdCursors = new Map();
  let noIdCursor = 0;

  // Walk segments through the same attempt-aware occurrence grouper as the
  // tool rows. Terminal deliverables stay outside the timeline.
  const batches = [];
  const timelineSegments = segments.filter((segment) => (
    segment && !segment.terminal
    && !(segment.type === "text" && segment.deliverable)
  ));
  const timelineHasLlm = timelineSegments.some(
    (segment) => toolExecutionLlmRound(segment) != null,
  );
  for (const group of computeExecutionBatches(timelineSegments, timelineHasLlm)) {
    const cur = {
      key: group.key, llmRound: group.llmRound, segs: [], rounds: [],
      attemptOrdinal: group.attemptOrdinal, totalAttempts: group.totalAttempts,
    };
    batches.push(cur);
    for (const s of group.items) {
      if (s.type === "tool_use") {
        if (s.id && _collapsedTodoTcIds.has(String(s.id))) continue;
        // A superseded-orphan tool_use (its round was dropped from realRounds
        // above): skip it entirely — no chip, and DON'T consume a positional
        // no-id round for it (that would mis-pair an unrelated body).
        if (s.id && _supersededTcIds.has(String(s.id))) continue;
        cur.segs.push(s);
        // Resolve the render-rich round for this tool_use.
        let r = null;
        if (s.id) {
          const callId = String(s.id);
          const queue = byId.get(callId);
          const occurrence = byIdCursors.get(callId) || 0;
          if (queue && occurrence < queue.length) {
            r = queue[occurrence];
            byIdCursors.set(callId, occurrence + 1);
          }
        }
        if (!r && noIdCursor < noId.length) r = noId[noIdCursor++];
        if (r) cur.rounds.push(r);
      } else {
        cur.segs.push(s);
      }
    }
  }
  if (batches.length === 0) return "";

  // If NO batch resolved any tool round (segments present but toolRounds
  // absent/unmatchable), the timeline would show prose with no tools —
  // fall back to the legacy path rather than render a lopsided view.
  const anyTool = batches.some((b) => b.rounds.length > 0);
  if (!anyTool && realRounds.length > 0) return "";

  /* Render each inject chip through the same retained dispatcher and typed
   * injection presenter so settled and live/grouped markup stay identical. */
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
  const panel = presentToolExecutionPanel(realRounds, allRounds, anyActive, t, escapeHtml, Icon('chevronDown', 16));
  return `<div class="ptool-panel seg-timeline${panel.active ? " ptool-panel-active" : ""}${panel.collapsed ? " collapsed" : ""}"${!panel.active ? ' data-collapsible="true"' : ''} data-attention="${panel.attention}">` +
    panel.html +
    `<div class="ptool-panel-body" data-full-count="${realRounds.length}">${inner}</div>` +
    `</div>`;
}

function _renderUnifiedGroup(allRounds, segments) {
  allRounds = _projectTodoRoundsForDisplay(allRounds);
  const anyActive = allRounds.some((r) => r.status === "searching" || r._swarmActive);
  /* The header counts REAL tool rounds only — never the display-only
   *   inject chips (swarm-inbox / peer / steer / stall-nudge) that ride the
   *   panel for chronological context. They are not calls the model made, and
   *   counting them lied twice over: a hydrated turn reported N+chips tools,
   *   and a TRIMMED turn (toolRounds stripped for transport, only the
   *   `_stallNudges` sidecar survived) rendered its lone chip as "使用了 1 个
   *   工具" (conv msg0cop6qf64ee — 32 real rounds hidden behind the trim).
   *   Parity with the segment-timeline path (renderSegmentTimelineHTML),
   *   which already counts realRounds only. When ONLY chips remain the count
   *   header is omitted entirely — "0 tools used" is not a useful claim; the
   *   trimmed-activity affordance below the panel carries the real count. */
  const realRounds = allRounds.filter((r) => r && !r._inboxInject
    && !r._peerInject && !r._userSteerInject && !r._bgCommandInject && !r._stallNudge
    && !r._programSynthetic);
  const count = allRounds.length;
  const panel = presentToolExecutionPanel(realRounds, allRounds, anyActive, t, escapeHtml, Icon('chevronDown', 16));
  const headerHtml = realRounds.length ? panel.html : "";
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
  return `<div class="ptool-panel${panel.active ? " ptool-panel-active" : ""}${panel.collapsed ? " collapsed" : ""}"${realRounds.length && !panel.active ? ' data-collapsible="true"' : ''} data-attention="${panel.attention}">
       ${headerHtml}
       <div class="ptool-panel-body" data-full-count="${realRounds.length}">${truncHtml}${lines}</div>
     </div>`;
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
  // Authoritative signal is the 'superseded' badge (ONLY
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
  if (!els.length) return false;
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
  return true;
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
  const commandTimerActive = _tickCmdTimers();
  const watcherTimerActive = typeof _tickTimerCountdowns === 'function'
    ? _tickTimerCountdowns() : false;
  return commandTimerActive || watcherTimerActive;
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
