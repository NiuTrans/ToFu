/* ═══════════════════════════════════════════════════════════════════
   mobile_panels.js — make desktop-anchored popovers usable on phones.

   Two problems this solves on mobile (≤768px):

   1. The Timer (#timerPanel) and Optimizer (#optimizerPanel) panels are
      DOM CHILDREN of their topbar badges (#timerBadge / #optimizerBadge),
      which are `display:none` on mobile. A child of a display:none parent
      never renders, so toggling `.visible` showed NOTHING. We PORTAL the
      panel element to <body> and present it as a bottom sheet with a
      backdrop. The panels' refresh-by-id logic (_refreshTimerPanel /
      _refreshOptimizerPanel look the content up by id) is untouched, so
      data still loads correctly wherever the panel lives.

   2. The Orchestration Flow selector (#flowToggle → #flowMenu radio list)
      is hidden inside the Mode submenu on mobile. We expose a mobile flow
      picker that reuses the SAME item set as the desktop _populateFlowMenu
      and the SAME setActiveFlow() state machine — no duplicated logic.

   Loaded after timer.js / optimizer.js / main_toolbar_ui.js (it wraps
   their globals); registered in lib/js_bundler.py _BUNDLE_FILES.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  "use strict";

  /* Mobile-view predicate: delegate to the shared core.js source of truth
   * (window.isMobileViewport / TOFU_BP.mobile) so this file no longer carries
   * its own 768 constant. Fallback keeps it self-contained if core is absent. */
  function _isMobileView() {
    return (typeof window.isMobileViewport === 'function')
      ? window.isMobileViewport()
      : window.innerWidth <= 768;
  }

  // ── Shared backdrop for portaled panels ────────────────────────────
  function _ensureBackdrop() {
    var bd = document.getElementById("mobilePanelBackdrop");
    if (!bd) {
      bd = document.createElement("div");
      bd.id = "mobilePanelBackdrop";
      bd.className = "mobile-panel-backdrop";
      bd.addEventListener("click", _closeAllMobilePanels);
      document.body.appendChild(bd);
    }
    return bd;
  }

  /* Remember where a portaled panel came from so we can put it back when
   * the viewport returns to desktop (resize) — keeps the desktop popover
   * behaviour intact after a phone rotate / devtools resize. */
  var _portaled = {};  // panelId → { parent, nextSibling }

  function _portalToBody(panelId) {
    var panel = document.getElementById(panelId);
    if (!panel) return null;
    if (!_portaled[panelId] && panel.parentNode && panel.parentNode.id !== "body-portal-host") {
      _portaled[panelId] = { parent: panel.parentNode, next: panel.nextSibling };
    }
    if (panel.parentNode !== document.body) {
      document.body.appendChild(panel);
    }
    panel.classList.add("mobile-panel-portaled");
    return panel;
  }

  function _restoreFromBody(panelId) {
    var rec = _portaled[panelId];
    var panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.remove("mobile-panel-portaled", "visible");
    if (rec && rec.parent) {
      try {
        if (rec.next && rec.next.parentNode === rec.parent) {
          rec.parent.insertBefore(panel, rec.next);
        } else {
          rec.parent.appendChild(panel);
        }
      } catch (e) {
        console.warn("[mobile_panels] restore failed for %s: %s", panelId, e && e.message);
      }
    }
    delete _portaled[panelId];
  }

  function _anyMobilePanelOpen() {
    return document.querySelector(".mobile-panel-portaled.visible")
      || document.querySelector("#mobileFlowSheet.open");
  }

  function _closeAllMobilePanels() {
    // Timer / optimizer portaled panels
    var t = document.getElementById("timerPanel");
    if (t && t.classList.contains("mobile-panel-portaled")) {
      t.classList.remove("visible");
      if (typeof window._setTimerPanelOpen === "function") window._setTimerPanelOpen(false);
    }
    var o = document.getElementById("optimizerPanel");
    if (o && o.classList.contains("mobile-panel-portaled")) {
      o.classList.remove("visible");
      if (typeof window._setOptimizerPanelOpen === "function") window._setOptimizerPanelOpen(false);
    }
    // Flow sheet
    var fs = document.getElementById("mobileFlowSheet");
    if (fs) fs.classList.remove("open");
    var bd = document.getElementById("mobilePanelBackdrop");
    if (bd) bd.classList.remove("open");
  }

  // ── Generic "open a portaled panel as a bottom sheet" ───────────────
  function _openPortaledPanel(panelId, refreshFn) {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    var panel = _portalToBody(panelId);
    if (!panel) return;
    _ensureBackdrop().classList.add("open");
    panel.classList.add("visible");
    if (typeof refreshFn === "function") {
      try { refreshFn(); } catch (e) { console.warn("[mobile_panels] refresh failed:", e && e.message); }
    }
  }

  // ── Timer: wrap toggleTimerPanel for mobile ─────────────────────────
  if (typeof window.toggleTimerPanel === "function") {
    var _origToggleTimer = window.toggleTimerPanel;
    window.toggleTimerPanel = function (e) {
      if (!_isMobileView()) return _origToggleTimer.call(this, e);
      if (e && e.stopPropagation) e.stopPropagation();
      var panel = document.getElementById("timerPanel");
      if (!panel) return;
      var isOpen = panel.classList.contains("visible") && panel.classList.contains("mobile-panel-portaled");
      if (isOpen) {
        _closeAllMobilePanels();
      } else {
        _openPortaledPanel("timerPanel",
          typeof _refreshTimerPanel === "function" ? _refreshTimerPanel : null);
        if (typeof window._setTimerPanelOpen === "function") window._setTimerPanelOpen(true);
      }
    };
  }

  // ── Optimizer: wrap toggleOptimizerPanel for mobile ─────────────────
  if (typeof window.toggleOptimizerPanel === "function") {
    var _origToggleOpt = window.toggleOptimizerPanel;
    window.toggleOptimizerPanel = function (e) {
      if (!_isMobileView()) return _origToggleOpt.call(this, e);
      if (e && e.stopPropagation) e.stopPropagation();
      var panel = document.getElementById("optimizerPanel");
      if (!panel) return;
      var isOpen = panel.classList.contains("visible") && panel.classList.contains("mobile-panel-portaled");
      if (isOpen) {
        _closeAllMobilePanels();
      } else {
        _openPortaledPanel("optimizerPanel",
          typeof _refreshOptimizerPanel === "function" ? _refreshOptimizerPanel : null);
        if (typeof window._setOptimizerPanelOpen === "function") window._setOptimizerPanelOpen(true);
      }
    };
  }

  // ── Mobile entry points (called from the #mobileSheet items) ────────
  window.openMobileTimer = function () {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    window.toggleTimerPanel();
  };
  window.openMobileOptimizer = function () {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    window.toggleOptimizerPanel();
  };

  // ── Mobile Flow picker ──────────────────────────────────────────────
  // Reuses the desktop item set + setActiveFlow() state machine.
  async function _buildFlowItems() {
    // Mirror _populateFlowMenu's item construction, sourcing custom flows
    // from the same Api.orchestrations.list() + _orchFlowCache.
    var cache = (typeof _orchFlowCache !== "undefined" && _orchFlowCache) ? _orchFlowCache : null;
    try {
      var custom = await Api.orchestrations.list();
      cache = (custom || []).map(function (e) { return { id: e.id, name: e.name || "Untitled" }; });
      if (typeof _orchFlowCache !== "undefined") { try { _orchFlowCache = cache; } catch (_e) {} }
    } catch (err) {
      console.warn("[mobile_panels] flow list failed:", err && err.message);
      cache = cache || [];
    }
    var items = [
      { flow: "", name: t("toolbar.flowNone"), desc: t("toolbar.flowNoneDesc") },
      { flow: "builtin:endpoint", name: t("toolbar.autonomousMode"), desc: t("toolbar.autonomousModeDesc") },
      { flow: "builtin:autopilot", name: t("toolbar.autopilot"), desc: t("toolbar.autopilotDesc") },
    ];
    for (var i = 0; i < cache.length; i++) {
      items.push({ flow: "" + cache[i].id, name: cache[i].name, desc: t("toolbar.flowCustomDesc") });
    }
    return items;
  }

  function _ensureFlowSheet() {
    var sheet = document.getElementById("mobileFlowSheet");
    if (!sheet) {
      sheet = document.createElement("div");
      sheet.id = "mobileFlowSheet";
      sheet.className = "mobile-bottom-sheet mobile-flow-sheet";
      sheet.innerHTML =
        '<div class="mobile-sheet-header" id="mobileFlowSheetTitle">' + escapeHtml(t("toolbar.flow")) + "</div>" +
        '<div class="mobile-sheet-section" id="mobileFlowSheetList" role="listbox" aria-labelledby="mobileFlowSheetTitle"></div>';
      document.body.appendChild(sheet);
    }
    return sheet;
  }

  window.openMobileFlowPicker = async function () {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    var sheet = _ensureFlowSheet();
    var list = document.getElementById("mobileFlowSheetList");
    var cur = (typeof activeFlow !== "undefined" && activeFlow) ? activeFlow : "";
    var items = await _buildFlowItems();
    list.innerHTML = items.map(function (it) {
      var sel = (it.flow === cur) ? " active" : "";
      var flowAttr = escapeHtml(it.flow);
      return '<div class="mobile-sheet-item' + sel + '" role="option" aria-selected="' + (it.flow === cur ? "true" : "false") + '" data-flow="' + flowAttr + '">' +
        '<span class="mobile-sheet-item-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><path d="M10 6.5h4a2 2 0 0 1 2 2V14"/></svg></span>' +
        '<span class="mobile-sheet-item-text">' +
        '<span class="mobile-sheet-item-name">' + escapeHtml(it.name) + "</span>" +
        '<span class="mobile-sheet-item-desc">' + escapeHtml(it.desc) + "</span>" +
        "</span>" +
        '<span class="mobile-sheet-item-check">✓</span>' +
        "</div>";
    }).join("");
    // Delegate taps → setActiveFlow (the real state machine), then close.
    list.onclick = function (ev) {
      var item = ev.target.closest(".mobile-sheet-item");
      if (!item) return;
      var flow = item.dataset.flow || "";
      if (typeof setActiveFlow === "function") setActiveFlow(flow);
      if (typeof updateMobileSheet === "function") updateMobileSheet();
      _closeAllMobilePanels();
    };
    _ensureBackdrop().classList.add("open");
    sheet.classList.add("open");
  };

  // ── Keep desktop behaviour after a resize back to wide ──────────────
  window.addEventListener("resize", function () {
    if (_isMobileView()) return;
    // Returned to desktop — close any mobile presentation and restore the
    // portaled panels to their original badge parents so the desktop
    // popover positioning works again.
    _closeAllMobilePanels();
    _restoreFromBody("timerPanel");
    _restoreFromBody("optimizerPanel");
  });

  // ── Escape closes the topmost mobile panel ──────────────────────────
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && _anyMobilePanelOpen()) _closeAllMobilePanels();
  });
})();
