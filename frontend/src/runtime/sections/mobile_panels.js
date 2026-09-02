/* ===== migrated source: mobile_panels.js ===== */
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
   their globals); registered in Vite's module graph the migrated module graph.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  "use strict";

  /* Mobile-view predicate: delegate to the shared core.js source of truth
   * (runtimeScope.isMobileViewport / TOFU_BP.mobile) so this file no longer carries
   * its own 768 constant. Fallback keeps it self-contained if core is absent. */
  function _isMobileView() {
    return (typeof runtimeScope.isMobileViewport === 'function')
      ? runtimeScope.isMobileViewport()
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
  var _flowPickerGeneration = 0;

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
    _flowPickerGeneration++;
    // Timer / optimizer portaled panels
    var t = document.getElementById("timerPanel");
    if (t && t.classList.contains("mobile-panel-portaled")) {
      t.classList.remove("visible");
      if (typeof runtimeScope._setTimerPanelOpen === "function") runtimeScope._setTimerPanelOpen(false);
    }
    var o = document.getElementById("optimizerPanel");
    if (o && o.classList.contains("mobile-panel-portaled")) {
      o.classList.remove("visible");
      if (typeof runtimeScope._setOptimizerPanelOpen === "function") runtimeScope._setOptimizerPanelOpen(false);
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

  // ── Timer/Optimizer: wrap the panel toggles for mobile ─────────────
  /* The wrap is re-runnable and identity-tracked: timer.js / optimizer.js are
   * retained runtime islands, so at boot the runtime service is the bridge stub.
   * The domain-loaded event re-captures the real implementation after its
   * explicit Vite owner has prepared the island. */
  var _capturedImpl = {};
  var _installedWrap = {};

  function _wrapOne(name, panelId, refreshName, setOpenName) {
    var cur = runtimeScope[name];
    if (typeof cur !== "function") return;
    if (_capturedImpl[name] === cur || _installedWrap[name] === cur) return;
    _capturedImpl[name] = cur;
    var wrapped = function (e) {
      if (!_isMobileView()) return _capturedImpl[name].call(this, e);
      if (e && e.stopPropagation) e.stopPropagation();
      var panel = document.getElementById(panelId);
      if (!panel) return;
      var isOpen = panel.classList.contains("visible") && panel.classList.contains("mobile-panel-portaled");
      if (isOpen) {
        _closeAllMobilePanels();
      } else {
        _openPortaledPanel(panelId,
          typeof runtimeScope[refreshName] === "function" ? runtimeScope[refreshName] : null);
        // @ts-expect-error -- string-keyed dynamic window wrap (typeof-guarded above)
        if (typeof runtimeScope[setOpenName] === "function") runtimeScope[setOpenName](true);
        // The panel is a required classic island. Ask the Vite bridge to
        // prepare its explicit owner; there is no all-feature rollback.
        if (typeof runtimeScope[refreshName] !== "function" && window.TofuModules &&
            typeof window.TofuModules.prepareFeature === "function") {
          window.TofuModules.prepareFeature(name).then(function () {
            // @ts-expect-error -- string-keyed dynamic window wrap (typeof-guarded)
            if (typeof runtimeScope[refreshName] === "function") runtimeScope[refreshName]();
          }).catch(function (err) {
            console.error('[mobile_panels] failed to prepare ' + name + ':', err);
          });
        }
      }
    };
    _installedWrap[name] = wrapped;
    runtimeScope[name] = wrapped;
  }

  function _wrapPanelToggles() {
    _wrapOne("toggleTimerPanel", "timerPanel", "_refreshTimerPanel", "_setTimerPanelOpen");
    _wrapOne("toggleOptimizerPanel", "optimizerPanel", "_refreshOptimizerPanel", "_setOptimizerPanelOpen");
  }
  _wrapPanelToggles();
  document.addEventListener("tofu:feature-domain-loaded", _wrapPanelToggles);

  // ── Mobile entry points (called from the #mobileSheet items) ────────
  runtimeScope.openMobileTimer = function () {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    runtimeScope.toggleTimerPanel();
  };
  runtimeScope.openMobileOptimizer = function () {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    runtimeScope.toggleOptimizerPanel();
  };

  // ── Mobile saved-workflow picker (Debug-only) ───────────────────────
  // Reuses the shared item/icon presentation + setActiveFlow() state machine.
  // Standard / Goal / Autopilot live in the parent Agent Mode sheet, so the
  // nested picker deliberately contains custom Studio definitions only.

  function _ensureFlowSheet() {
    var sheet = document.getElementById("mobileFlowSheet");
    if (!sheet) {
      sheet = document.createElement("div");
      sheet.id = "mobileFlowSheet";
      sheet.className = "mobile-bottom-sheet mobile-flow-sheet";
      sheet.innerHTML =
        '<div class="mobile-sheet-header" id="mobileFlowSheetTitle">' + escapeHtml(t("toolbar.savedWorkflows")) + "</div>" +
        '<div class="flow-catalog-notice" id="mobileFlowSheetStatus" role="status" aria-live="polite" aria-atomic="true" hidden></div>' +
        '<div class="mobile-sheet-section" id="mobileFlowSheetList" role="radiogroup" aria-labelledby="mobileFlowSheetTitle"></div>' +
        '<button type="button" class="mobile-workflow-manage" id="mobileManageWorkflows">' + escapeHtml(t("toolbar.manageWorkflows")) + '</button>';
      document.body.appendChild(sheet);
      sheet.querySelector('#mobileManageWorkflows').addEventListener(
        'click', function () {
          _closeAllMobilePanels();
          if (typeof openOrchestrationFromAgentMode === 'function') {
            openOrchestrationFromAgentMode();
          }
        });
    }
    return sheet;
  }

  function _renderMobileFlowItems(list, custom, current) {
    if (!list || typeof projectOrchestrationFlowPickerItems !== "function") {
      return false;
    }
    var catalog = typeof _orchestrationFlowCatalog !== "undefined"
      ? _orchestrationFlowCatalog : null;
    var catalogNoticeVisible = false;
    if (typeof renderOrchestrationFlowCatalogNotice === "function") {
      catalogNoticeVisible = renderOrchestrationFlowCatalogNotice(
        document.getElementById("mobileFlowSheetStatus"), catalog, t);
    }
    var items = typeof projectOrchestrationFlowPickerItems === 'function'
      ? projectOrchestrationFlowPickerItems(custom, t, {
        includeNone: false,
        includeBuiltins: false,
      }) : [];
    list.innerHTML = items.length ? items.map(function (it) {
      var sel = (it.flow === current) ? " active" : "";
      var flowAttr = escapeHtml(it.flow);
      return '<button type="button" class="mobile-sheet-item' + sel + '" role="radio" aria-checked="' + (it.flow === current ? "true" : "false") + '" aria-selected="' + (it.flow === current ? "true" : "false") + '" data-flow="' + flowAttr + '">' +
        '<span class="mobile-sheet-item-icon">' + orchestrationFlowPickerIcon(it.flow) + '</span>' +
        '<span class="mobile-sheet-item-text">' +
        '<span class="mobile-sheet-item-name">' + escapeHtml(it.name) + "</span>" +
        '<span class="mobile-sheet-item-desc">' + escapeHtml(it.desc) + "</span>" +
        "</span>" +
        '<span class="mobile-sheet-item-check">✓</span>' +
        "</button>";
    }).join("") : (catalogNoticeVisible ? ''
      : '<div class="mobile-workflow-empty">' +
        escapeHtml(t("toolbar.noSavedWorkflows")) + '</div>');
    if (typeof wireOrchestrationFlowPicker === "function") {
      wireOrchestrationFlowPicker(list, {
        onSelect: function (flow) {
          if (typeof setActiveFlow === "function") setActiveFlow(flow);
          if (typeof updateMobileSheet === "function") updateMobileSheet();
          _closeAllMobilePanels();
        },
        onEscape: _closeAllMobilePanels,
      });
    }
    return true;
  }

  runtimeScope.openMobileFlowPicker = async function () {
    if (typeof closeMobileSheet === "function") closeMobileSheet();
    var sheet = _ensureFlowSheet();
    var list = document.getElementById("mobileFlowSheetList");
    var cur = (typeof activeFlow !== "undefined" && activeFlow) ? activeFlow : "";
    var owner = ++_flowPickerGeneration;
    var catalog = typeof _orchestrationFlowCatalog !== "undefined"
        && _orchestrationFlowCatalog ? _orchestrationFlowCatalog : null;
    var request = catalog ? catalog.load() : Promise.resolve([]);
    _renderMobileFlowItems(list, catalog ? catalog.snapshot() : [], cur);
    _ensureBackdrop().classList.add("open");
    sheet.classList.add("open");
    var custom = await request;
    if (owner !== _flowPickerGeneration
        || !sheet.classList.contains("open")) return false;
    cur = (typeof activeFlow !== "undefined" && activeFlow) ? activeFlow : "";
    return _renderMobileFlowItems(list, custom, cur);
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
