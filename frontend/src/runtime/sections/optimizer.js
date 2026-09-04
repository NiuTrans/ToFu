/* ===== migrated source: optimizer.js ===== */
/* ═══════════════════════════════════════════
   optimizer.js — Daily Optimizer panel
   Shows today's optimizer proposals, applied changes, and reverts.
   Mirrors the scheduler badge / panel UX.
   ═══════════════════════════════════════════ */

let _optimizerPanelOpen = false;
let _optimizerPollTimer = null;
let _optimizerLastRefresh = 0;

function toggleOptimizerPanel(e) {
  const panel = document.getElementById("optimizerPanel");
  if (!panel) return;
  // The panel is a DOM descendant of the badge, so clicks anywhere inside
  // the panel bubble up here. Without this guard, clicking a row, button,
  // or the "older proposals" header would flip _optimizerPanelOpen back to
  // false and close the popup.
  if (e && panel.contains(/** @type {Node} */ (e.target))) return;
  if (e) e.stopPropagation();
  _optimizerPanelOpen = !_optimizerPanelOpen;
  panel.classList.toggle("visible", _optimizerPanelOpen);
  if (_optimizerPanelOpen) _refreshOptimizerPanel();
}

function _optFmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso.slice(0, 16);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? d.toLocaleTimeString() : d.toLocaleString();
  } catch (e) {
    return iso.slice(0, 16);
  }
}

function _optStatusIcon(status) {
  switch (status) {
    case "applied": return IconDot('green');
    case "pending_review": return IconDot('yellow');
    case "rejected": return IconDot('red');
    case "expired": return IconDot('grey');
    case "reverted": return Icon('refresh', 12);
    default: return IconDot('grey');
  }
}

function _optSeverityColor(sev) {
  if (sev === "high") return "#f87171";
  if (sev === "med") return "#fbbf24";
  return "#60a5fa";
}

// Map the backend's English status_reason strings (from
// lib/optimizer/applier.py and routes/optimizer.py) to translated text.
// Falls back to the raw string for unrecognised reasons.
function _optStatusReasonText(reason) {
  if (!reason) return "";
  if (reason === "not in auto-apply whitelist") return t('optimizer.reason.notInWhitelist');
  if (reason === "dry_run") return t('optimizer.reason.dryRun');
  if (reason.indexOf("unknown action_type:") === 0) {
    const type = reason.slice("unknown action_type:".length).trim();
    return t('optimizer.reason.unknownAction', { type: type });
  }
  if (reason.indexOf("manual approve blocked") === 0) {
    return t('optimizer.reason.manualApproveBlocked');
  }
  return reason;
}

function _optRenderAction(p) {
  const args = p.action_args || {};
  const type = p.action_type || "other";
  if (type === "block_search_domain" && args.domain) {
    const ttl = args.ttl_days ? ` · ${args.ttl_days}d TTL` : "";
    return `${t('optimizer.blockSearchDomain')} <b>${escapeHtml(args.domain)}</b>${ttl}`;
  }
  if (type === "other") {
    return `${Icon('lightbulb', 13)} ${escapeHtml(p.title || t('optimizer.untitled'))}`;
  }
  // Fall-through: show type + first arg
  const firstArg = Object.keys(args).slice(0, 2).map(k => `${k}=${JSON.stringify(args[k]).slice(0, 30)}`).join(", ");
  return `${Icon('cog', 13)} ${escapeHtml(type)}${firstArg ? " <span style='color:var(--text-secondary)'>" + escapeHtml(firstArg) + "</span>" : ""}`;
}

function _optimizerFeatureEnabled() {
  var flags = runtimeScope._featureFlags;
  if (!flags) return true; // optimistic pre-load
  return flags.optimizer_enabled !== false;
}

async function _refreshOptimizerPanel() {
  try {
    if (!_optimizerFeatureEnabled()) {
      const countEl = document.getElementById("optimizerCount");
      if (countEl) countEl.style.display = "none";
      const badgeEl = document.getElementById("optimizerBadge");
      if (badgeEl) badgeEl.style.display = "none";
      const content = document.getElementById("optimizerPanelContent");
      if (content) content.innerHTML = '<div class="optimizer-panel-empty">' + t('optimizer.disabled') + '</div>';
      return;
    }
    const data = await Api.optimizer.proposals(60);
    if (!data || !data.ok) return;
    _optimizerLastRefresh = Date.now();

    const proposals = data.proposals || [];

    // Filter to "today" (server-local TZ matches client TZ closely enough for a badge count)
    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);
    const todays = proposals.filter(p => {
      try { return new Date(p.created_at) >= todayStart; } catch (e) { return false; }
    });

    const appliedToday = todays.filter(p => p.status === "applied");
    const pendingToday = todays.filter(p => p.status === "pending_review");
    const revertedToday = todays.filter(p => p.status === "reverted" || p.status === "expired");

    // Badge count = today's applied + pending (anything that changed behaviour today)
    const countEl = document.getElementById("optimizerCount");
    // The badge's display is driven by the feature flag (set on initial
    // feature-flags fetch + the Settings toggle).  Don't override it here.
    if (countEl) {
      const n = appliedToday.length + pendingToday.length;
      if (n > 0) {
        countEl.textContent = n;
        countEl.style.display = "inline-flex";
      } else {
        countEl.style.display = "none";
      }
    }

    const content = document.getElementById("optimizerPanelContent");
    if (!content) return;

    if (proposals.length === 0) {
      content.innerHTML = '<div class="optimizer-panel-empty">' + t('optimizer.empty') + '</div>' +
        '<div style="margin-top:10px;text-align:center"><button class="opt-run-btn" data-tofu-action="_optimizerRunNow()">' + t('optimizer.runNow') + '</button></div>';
      return;
    }

    // Group by section
    const sections = [
      { key: "applied", label: t('optimizer.appliedToday'), items: appliedToday },
      { key: "pending", label: t('optimizer.pendingReview'), items: pendingToday },
      { key: "reverted", label: t('optimizer.revertedToday'), items: revertedToday },
    ];
    let html = "";

    // Header: last run time + manual-run button
    html += `<div class="opt-toolbar">
      <span class="opt-toolbar-info">${t('optimizer.lastRefresh')}${new Date(_optimizerLastRefresh).toLocaleTimeString()}</span>
      <button class="opt-run-btn" data-tofu-action="_optimizerRunNow()" title="${t('optimizer.runNowTitle')}">${t('optimizer.runNow')}</button>
    </div>`;

    for (const sec of sections) {
      if (!sec.items.length) continue;
      html += `<div class="opt-section-title">${sec.label} <span class="opt-count">${sec.items.length}</span></div>`;
      for (const p of sec.items) {
        html += _renderOptimizerRow(p);
      }
    }

    // History — everything else (older than today)
    const older = proposals.filter(p => !todays.includes(p));
    if (older.length > 0) {
      html += `<div class="opt-section-title opt-history-header" data-tofu-action="_optToggleHistory()">
        ${t('optimizer.olderProposals')} <span class="opt-count">${older.length}</span>
        <span id="optHistoryArrow" style="float:right">▸</span>
      </div>`;
      html += '<div id="optHistoryBody" style="display:none">';
      for (const p of older.slice(0, 40)) {
        html += _renderOptimizerRow(p);
      }
      html += '</div>';
    }

    content.innerHTML = html;
  } catch (e) {
    console.warn("[Optimizer] Panel refresh failed:", e);
  }
}

function _optToggleHistory() {
  const body = document.getElementById("optHistoryBody");
  const arrow = document.getElementById("optHistoryArrow");
  if (!body) return;
  const open = body.style.display !== "none";
  body.style.display = open ? "none" : "block";
  if (arrow) arrow.textContent = open ? "▸" : "▾";
}

function _renderOptimizerRow(p) {
  const sev = p.severity || "low";
  const conf = typeof p.confidence === "number" ? Math.round(p.confidence * 100) : 50;
  const rationale = (p.rationale || "").slice(0, 260);
  const reasonText = _optStatusReasonText((p.status_reason || "").slice(0, 200));
  const reason = reasonText ? `<div class="opt-row-reason">${escapeHtml(reasonText)}</div>` : "";

  let actions = "";
  if (p.status === "pending_review") {
    actions = `
      <button class="opt-btn opt-btn-approve" data-tofu-action="_optApprove('${p.id}')" title="${t('optimizer.approveTitle')}">${t('optimizer.approve')}</button>
      <button class="opt-btn opt-btn-reject" data-tofu-action="_optReject('${p.id}')" title="${t('optimizer.rejectTitle')}">${t('optimizer.reject')}</button>
    `;
  } else if (p.status === "applied") {
    actions = `<button class="opt-btn opt-btn-revert" data-tofu-action="_optRevert('${p.id}')" title="${t('optimizer.revertTitle')}">${t('optimizer.revert')}</button>`;
  }

  return `<div class="opt-row opt-status-${p.status || 'unknown'}">
    <div class="opt-row-head">
      <span class="opt-status-icon">${_optStatusIcon(p.status)}</span>
      <span class="opt-action-summary">${_optRenderAction(p)}</span>
      <span class="opt-row-conf" style="color:${_optSeverityColor(sev)}" title="severity=${sev}, confidence=${conf}%">${sev.toUpperCase()} · ${conf}%</span>
    </div>
    <div class="opt-row-meta">
      <span>${_optFmtTime(p.created_at)}</span>
      ${p.title ? `· <span class="opt-row-title">${escapeHtml(p.title.slice(0, 90))}</span>` : ""}
    </div>
    ${rationale ? `<div class="opt-row-rationale">${escapeHtml(rationale)}${(p.rationale || "").length > 260 ? "…" : ""}</div>` : ""}
    ${reason}
    ${actions ? `<div class="opt-row-actions">${actions}</div>` : ""}
  </div>`;
}

async function _optApprove(id) {
  try {
    const data = await Api.optimizer.approve(id);
    if (data && data.ok) {
      debugLog(`🧭 Optimizer: proposal ${id.slice(0, 12)} approved & applied`, "success");
    } else {
      debugLog(`🧭 Approve failed: ${(data && data.error) || "unknown"}`, "error");
    }
  } catch (e) {
    debugLog(`🧭 Approve error: ${e.message}`, "error");
  }
  _refreshOptimizerPanel();
}

async function _optReject(id) {
  const reason = await showPrompt(t('optimizer.rejectPrompt'), { defaultValue: "" });
  try {
    const data = await Api.optimizer.reject(id, reason || "");
    if (data && data.ok) debugLog(`🧭 Optimizer: proposal ${id.slice(0, 12)} rejected`, "info");
    else debugLog(`🧭 Reject failed: ${(data && data.error) || "unknown"}`, "error");
  } catch (e) {
    debugLog(`🧭 Reject error: ${e.message}`, "error");
  }
  _refreshOptimizerPanel();
}

async function _optRevert(id) {
  if (!await showConfirm(t('optimizer.revertConfirm'), { danger: true })) return;
  try {
    const data = await Api.optimizer.revert(id);
    if (data && data.ok) debugLog(`🧭 Optimizer: proposal ${id.slice(0, 12)} reverted`, "success");
    else debugLog(`🧭 Revert failed: ${(data && data.error) || "unknown"}`, "error");
  } catch (e) {
    debugLog(`🧭 Revert error: ${e.message}`, "error");
  }
  _refreshOptimizerPanel();
}

async function _optimizerRunNow() {
  const btn = event && event.target;
  if (btn) { btn.disabled = true; btn.textContent = t('optimizer.running'); }
  try {
    const data = await Api.optimizer.runNow({ dry_run: false, window_hours: 24 });
    if (data && data.ok) {
      const s = data.summary || {};
      debugLog(
        `🧭 Optimizer: produced=${(s.proposals || []).length} applied=${(s.applied || []).length} pending=${(s.pending_review || []).length} reverts=${(s.reverts || []).length}`,
        "success"
      );
    } else {
      debugLog(`🧭 Run failed: ${(data && data.error) || "unknown"}`, "error");
    }
  } catch (e) {
    debugLog(`🧭 Run error: ${e.message}`, "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t('optimizer.runNow'); }
    _refreshOptimizerPanel();
  }
}

// Auto-refresh while panel is open (and once on load to populate the badge)
function _startOptimizerPolling() {
  if (_optimizerPollTimer) return;
  _optimizerPollTimer = setInterval(() => {
    // Refresh badge in background even when panel is closed (every 5 min)
    if (_optimizerPanelOpen || (Date.now() - _optimizerLastRefresh) > 5 * 60 * 1000) {
      _refreshOptimizerPanel();
    }
  }, 60000);
  // Initial fetch so the badge count is accurate a moment after boot
  setTimeout(_refreshOptimizerPanel, 2500);
}
_startOptimizerPolling();

// Allow mobile_panels.js to keep the open-flag in sync when it portals the
// panel to <body> as a bottom sheet.
if (typeof window !== "undefined") {
  runtimeScope._setOptimizerPanelOpen = function (v) { _optimizerPanelOpen = !!v; };
}

// Close panel when clicking outside
document.addEventListener("click", (e) => {
  if (!_optimizerPanelOpen) return;
  const panel = document.getElementById("optimizerPanel");
  // On mobile the panel is portaled out of the badge into <body>; its own
  // backdrop (mobile_panels.js) owns closing, so skip the badge-based check.
  if (panel && panel.classList.contains("mobile-panel-portaled")) return;
  const badge = document.getElementById("optimizerBadge");
  if (badge && !badge.contains(/** @type {Node} */ (e.target))) {
    _optimizerPanelOpen = false;
    if (panel) panel.classList.remove("visible");
  }
});
